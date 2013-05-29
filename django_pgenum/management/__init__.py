from django.db.models.signals import pre_syncdb
from ..enum import sync_enums


pre_syncdb.connect(sync_enums)
