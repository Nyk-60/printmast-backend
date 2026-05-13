from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models

from apps.core.models import TimeStampedModel


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError('Email is required')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    username = None
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=120, blank=True)
    shipstation_store_id = models.CharField(max_length=64, blank=True, db_index=True)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return self.email


class SubUser(TimeStampedModel):
    parent = models.ForeignKey(User, related_name='sub_users', on_delete=models.CASCADE)
    email = models.EmailField()
    role = models.CharField(max_length=32, default='operator')

    class Meta:
        unique_together = ('parent', 'email')
