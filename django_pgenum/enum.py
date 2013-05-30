import enum
import re
import pickle

from django.db import connections, models
from django.db.models import fields
from django.db.transaction import atomic

try:
    from south.modelsinspector import add_introspection_rules
except ImportError:
    add_introspection_rules = None


def _get_db_name(class_name):
    return re.sub(
        r"(((?<=[a-z])[A-Z])|([A-Z](?![A-Z]|$)))",
        ' \\1',
        class_name,
    ).lower().strip().replace(" ", "_")


class EnumCache(object):

    # Use the Borg pattern to share state between all instances. Details at
    # http://aspn.activestate.com/ASPN/Cookbook/Python/Recipe/66531.
    __shared_state = {
        "enums": {},
    }

    def __init__(self):
        self.__dict__ = self.__shared_state

    def add_enum(self, app, enum_cls):
        self.enums.setdefault(app, set()).add(enum_cls)

    def get_app_enums(self, app):
        for enum in self.enums.get(app, []):
            yield enum


enum_cache = EnumCache()


class EnumMeta(enum.EnumMeta):

    def __new__(metacls, cls, bases, classdict):
        temp = type(classdict)()
        names = set(classdict._enum_names)

        for k in classdict._enum_names:
            v = classdict[k]

            # If our item is just an ellipsis then set our internal value to
            #   (itemname, itemname)
            if v is Ellipsis:
                v = (k, k)

            # If our item is a list or tuple then set our internal value to
            #   a tuple of the items with Ellipsis expanded to the itemname
            if isinstance(v, (list, tuple)):
                v = tuple([k if x is Ellipsis else x for x in v])

            temp[k] = v

        # Pass through all of the items that are not enum members
        for k, v in classdict.items():
            if k not in names:
                temp[k] = v

        # Check to see if this should be an abstract enum
        if "__abstract__" not in temp:
            temp["__abstract__"] = False

        # Give the enum a database name if one doesn't exist
        if "__enumname__" not in temp:
            temp["__enumname__"] = _get_db_name(cls)

        # Create our enumeration using the normal EnumMeta.__new__
        enum_cls = super(EnumMeta, metacls).__new__(metacls, cls, bases, temp)

        # Register our enumeration into the EnumCache
        if not enum_cls.__abstract__:
            enum_cache.add_enum(enum_cls.__module__, enum_cls)

        return enum_cls

    @staticmethod
    def _find_new(classdict, obj_type, first_enum):
        def new(enum_class, db, display=None):
            vals = enum.EnumMeta._find_new(classdict, obj_type, first_enum)
            real_new, _, use_args = vals

            if not use_args:
                enum_item = real_new(enum_class)
                enum_item._value = db
            else:
                enum_item = real_new(enum_class, db)
                if not hasattr(enum_item, "_value"):
                    enum_item._value = obj_type(db)

            # Add the _display attribute
            enum_item.display = display if display is not None else db

            return enum_item

        return new, False, True


class Enum(enum.Enum, metaclass=EnumMeta):

    __abstract__ = True


def sync_enums(sender=None, app=None, db=None, verbosity=0, **kwargs):
    db = "default" if db is None else db

    with atomic(using=db):
        cursor = connections[db].cursor()

        # Locate all the currently defined enums
        cursor.execute("SELECT typname FROM pg_type WHERE typtype = 'e'")
        defined = set(x[0] for x in cursor.fetchall())

        for enum_cls in enum_cache.get_app_enums(app.__name__):
            if verbosity >= 3:
                print("Processing %s:%s enum" % (
                                        app.__name__, enum_cls.__name__))

            if enum_cls.__enumname__ in defined:
                continue

            sql = (
                "CREATE TYPE " + enum_cls.__enumname__ + " AS ENUM %s",
                (tuple(enum_cls.__members__.keys()),)
            )

            if verbosity >= 1:
                print("Creating enum %s" % enum_cls.__enumname__)

            cursor.execute(*sql)


class EnumField(fields.Field, metaclass=models.SubfieldBase):

    def __init__(self, enum, *args, **kwargs):
        self.enum = enum
        kwargs["choices"] = [(x.value, x.display) for x in self.enum]
        super(EnumField, self).__init__(*args, **kwargs)

    def db_type(self, connection):
        return self.enum.__enumname__

    def to_python(self, value):
        if isinstance(value, self.enum):
            return value

        if value is None:
            return value

        return self.enum[value]

    def get_prep_value(self, obj):
        return obj.value

    def value_to_string(self, obj):
        return obj.value

    def south_field_triple(self):
        return (
                "django_pgenum.enum.EnumField",
                [],
                {"enum": "pickle.loads(%s)" % pickle.dumps(self.enum)}
            )
