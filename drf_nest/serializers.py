from collections import OrderedDict
from urllib.parse import urlparse

from django.conf import settings
from django.urls import resolve
from django.contrib.auth.models import User, Group, AnonymousUser

from rest_framework import serializers
from rest_framework.fields import SkipField
from rest_framework.relations import PKOnlyObject, ManyRelatedField, HyperlinkedRelatedField


from drf_nest.serializer_fields import TypeField, ExtendedModelSerialiserField, PrivateSerialiserField


class PrivacyMixin():
    def to_representation(self, instance):
        ret = super(PrivacyMixin, self).to_representation(instance)

        # If user is superuser or serialisation was called internally return the full representation
        if self.context['request'].user.is_superuser or type(self.context['request'].user) == AnonymousUser:
            return ret
        user = User.objects.get(id=self.context['request'].user.id) #NOTE: This is not the same as user = self.context['request'].user as user.saleuserprofile.customer_ids produces different results
            
        # Get profile attributes
        groups = user.groups.all()
        customer_ids = user.saleuserprofile.customer_ids
        for group in groups:
            customer_ids = list(set(customer_ids + group.salegroupprofile.customer_ids))
        store_ids = []
        for group in groups:
            store_ids = list(set(store_ids + group.salegroupprofile.store_ids))

        # Check the private fields to see if user is allowed from either user profile or group profiles
        for field in self.fields.keys():
            if type(self.fields[field]) == PrivateSerialiserField:
                if self.fields[field].serializer_field_parent is None:
                    if getattr(instance, 'customer_id', '~~') in customer_ids or getattr(instance, 'store_id', '~~') in store_ids:
                        view_private = True
                    else:
                        ret.pop(field)
                else:
                    parent = getattr(instance, self.fields[field].serializer_field_parent)
                    if getattr(parent, 'customer_id', '~~') in customer_ids or getattr(parent, 'store_id', '~~') in store_ids:
                        view_private = True
                    else:
                        ret.pop(field)

        return ret
        
        
class LimitDepthMixin():
    """
    Mixin which overloads to_representation of serialiser to enforce a max depth when serialising nested relations.
    When max depth is reached the response will be the url value
    """
    
    def to_representation(self, instance):
        ret = OrderedDict()
        fields = self._readable_fields

        self.expansion_depth = getattr(self, "expansion_depth", 0)
        
        if self.expansion_depth >= settings.DEPTH_MAX:
            for field in fields:
                if field.field_name == self.url_field_name:
                    attribute = field.get_attribute(instance)
                    return field.to_representation(attribute)
                    
        for field in fields:
            field.expansion_depth = self.expansion_depth + 1
            try:
                attribute = field.get_attribute(instance)
            except SkipField:
                continue

            # For related fields with `use_pk_only_optimization` we need to
            # resolve the pk value.
            check_for_none = attribute.pk if isinstance(attribute, PKOnlyObject) else attribute
            if check_for_none is None:
                ret[field.field_name] = None
            else:
                ret[field.field_name] = field.to_representation(attribute)

        return ret


class ExtendedHyperlinkedSerialiser(serializers.HyperlinkedModelSerializer):
    """
        Serialiser for models with writable related fields.
        Related fields should deserialise to dicts as the parent instance may not be saved yet
        Includes a type field to allow fields specified in the serialiser to know how to serialise correctly.
        Derived from hyper-linked serialiser and the url field must be present on the serialiser
    """
    type = TypeField()
    

    def to_representation(self, instance):
        # TODO: Why is this returning an ordered dict?
        #if "{}".format(type(instance)) == "<class 'sample_project.app.models.SalesChannel'>":
        #    print("foo", super(ExtendedHyperlinkedSerialiser, self).to_representation(instance))
        ret = super(ExtendedHyperlinkedSerialiser, self).to_representation(instance)
        return ret
    
    def create(self, validated_data):
        # Remove nested fields from validated data and add to separate dict
        fields = []
        related = []
        for field in validated_data.keys():
            if type(self.fields[field]) == ExtendedModelSerialiserField:
                # Check if we have a URL so we can assign as part of creation
                if type(validated_data[field]) == dict:
                    if "url" not in validated_data[field]:
                        related.append(field)
                    else:
                        resolved_func, unused_args, resolved_kwargs = resolve(urlparse(validated_data[field]["url"]).path)
                        objects = resolved_func.cls.queryset.filter(pk=resolved_kwargs['pk'])
                        del validated_data[field]["url"]
                        objects.update(**validated_data[field])
                        validated_data[field] = objects[0]
                        fields.append(field)
            elif type(self.fields[field]) == ManyRelatedField:
                related.append(field)
            else:
                fields.append(field)

        # Create instance of serialiser Meta.model must get pk to attach related objects
        instance = self.Meta.model()
        for field in fields:
            setattr(instance, field, validated_data[field])
        instance.save()

        # For all related fields attach the listed objects
        for field in related:
            for obj_dict in validated_data[field]:
                attr = getattr(instance, field)

                if type(obj_dict) != dict:
                    # If we have an object then add it
                    object = obj_dict
                else:
                    # If we have a dictionary then create (TODO: or update)
                    object = self.fields[field].serializer.Meta.model(**obj_dict)
                    setattr(object, attr.field.name, instance)
                    object.save()

                attr.add(object)

        instance.save()
        return instance

        
    def update(self, instance, validated_data):
        # Create a list of fields to update and a list of related objects
        fields = []
        related = []
        for field in validated_data.keys():
            #print(field,type(self.fields[field]))
            if type(self.fields[field]) == ExtendedModelSerialiserField:
                if validated_data[field] is not None:
                    related.append(field)
            elif type(self.fields[field]) in (ManyRelatedField, HyperlinkedRelatedField):
                related.append(field)
            # Exclude read only fields
            elif not self.fields[field].read_only:
                fields.append(field)

        # Set all valid attributes of the instance to the validated data
        for field in fields:
            setattr(instance, field, validated_data.get( field, getattr(instance, field)))

        # Loop through sub objects, deserialise and add to parent
        for field in related:
            need_save = True
            if validated_data[field] != None:
                # Handle Foreign keys by creating list of 1 (many=False will result in dict not list of dicts)
                if type(validated_data[field]) != list:
                    validated_data[field] = [validated_data[field],]
                # For each sub object of the instance field there is a dict containing attributes and values or the instance
                for obj_dict in validated_data[field]:
                    attr = getattr(instance, field)
                    if type(obj_dict) != dict:
                        # If no fields specified in payload (just URL) then no need to save sub object
                        need_save = False
                        if type(obj_dict) == str:
                            resolved_func, unused_args, resolved_kwargs = resolve(urlparse(obj_dict).path)
                            object = resolved_func.cls.queryset.get(pk=resolved_kwargs['pk'])                    
                        else:
                            object = obj_dict
                    else:
                        # Input may specify an existing or new sub object. If existing, there must be a url field for us to look it up
                        if "url" in obj_dict.keys():
                            # Get object from url and update from deserialised dict
                            resolved_func, unused_args, resolved_kwargs = resolve(urlparse(obj_dict["url"]).path)
                            object = resolved_func.cls.queryset.get(pk=resolved_kwargs['pk'])
                            serializer = self.fields[field].serializer
                            
                            # If only the url is in the data then no need to update the sub-object fields or save
                            if len(obj_dict.keys()) > 1:
                                serializer.fields
                                val = serializer.validate(obj_dict)
                                serializer.update(object,val)
                            else:
                                need_save = False
                        else:
                            # Create object from deserialised dict
                            object = self.fields[field].serializer.Meta.model(**obj_dict)
                            # Note: Do not save new object until we have set the attribute to the parent depending on relationship type

                    if need_save:
                        # Handle all types of relationships
                        if attr.__class__.__name__ == "ManyRelatedManager":
                            # ManyToMany
                            object.save()
                            getattr(instance,field).add(object)
                        elif attr.__class__.__name__ == "RelatedManager":
                            # Reverse relationship to foreign keys
                            setattr(object, attr.field.name, instance)
                            object.save()
                            attr.add(object) #TODO: Check if this works for both lists and singles
                        else:
                            # Foreign keys
                            object.save()
                            setattr(instance, field, object)
                        #TODO: Removal of missing related objects if partial
                    
        instance.save()
        return instance
