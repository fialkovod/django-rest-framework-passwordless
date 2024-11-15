import logging
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import (get_user_model, login as django_login)
from django.core.exceptions import PermissionDenied
from django.core.validators import RegexValidator
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from drfpasswordless.models import CallbackToken
from drfpasswordless.settings import api_settings
from drfpasswordless.utils import verify_user_alias, validate_token_age
import requests, random, string

logger = logging.getLogger(__name__)
User = get_user_model()


class TokenField(serializers.CharField):
    default_error_messages = {
        'required': _('Invalid Token'),
        'invalid': _('Invalid Token'),
        'blank': _('Invalid Token'),
        'max_length': _('Tokens are {max_length} digits long.'),
        'min_length': _('Tokens are {min_length} digits long.')
    }


class AbstractBaseAliasAuthenticationSerializer(serializers.Serializer):
    """
    Abstract class that returns a callback token based on the field given
    Returns a token if valid, None or a message if not.
    """

    @property
    def alias_type(self):
        # The alias type, either email or mobile
        raise NotImplementedError

    @property
    def alias_field_name(self):
        # The alias field name, either email or mobile
        raise NotImplementedError
    
    def generate_unique_username(selfS):
      # Генерация случайной строки длиной 8 символов
      random_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
      unique_username = f"user_{random_suffix}"

      # Проверка на уникальность
      while User.objects.filter(username=unique_username).exists():
          random_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
          unique_username = f"user_{random_suffix}"

      return unique_username

    def validate(self, attrs):
        alias = attrs.get(self.alias_type)


        if api_settings.PASSWORDLESS_USE_RECAPTCHA is True:
            captcha_token = attrs.get('captcha_token')

            if captcha_token:
                data = {
                    'secret': api_settings.PASSWORDLESS_RECAPTCHA_SECRET_KEY,
                    'response': captcha_token
                }
                try:
                    r = requests.post('https://www.google.com/recaptcha/api/siteverify', data=data)
                    result = r.json()
                    if result['success'] is not True:
                        msg = _('Recaptcha failed.')
                        raise serializers.ValidationError(msg)                        
                except:
                    msg = _('Can\'t check recaptcha token.')
                    raise serializers.ValidationError(msg)
            else:
                msg = _('No captcha token provided.')
                raise serializers.ValidationError(msg)                


        if alias:
            # Create or authenticate a user
            # Return THem

            if api_settings.PASSWORDLESS_REGISTER_NEW_USERS is True:
                # If new aliases should register new users.
                try:
                    user = User.objects.get(**{self.alias_field_name+'__iexact': alias})
                except User.DoesNotExist:
                    username = self.generate_unique_username()
                    user = User.objects.create(username=username, **{self.alias_field_name: alias})
                    user.set_unusable_password()
                    user.save()
            else:
                # If new aliases should not register new users.
                try:
                    user = User.objects.get(**{self.alias_field_name+'__iexact': alias})
                except User.DoesNotExist:
                    user = None

            if user:
                if not user.is_active:
                    # If valid, return attrs so we can create a token in our logic controller
                    msg = _('User account is disabled.')
                    raise serializers.ValidationError(msg)
            else:
                msg = _('No account is associated with this alias.')
                raise serializers.ValidationError(msg)
        else:
            msg = _('Missing %s.') % self.alias_field_name
            raise serializers.ValidationError(msg)

        attrs['user'] = user
        return attrs


class EmailAuthSerializer(AbstractBaseAliasAuthenticationSerializer):
    @property
    def alias_type(self):
        return 'email'

    @property
    def alias_field_name(self):
        return api_settings.PASSWORDLESS_USER_EMAIL_FIELD_NAME

    email = serializers.EmailField()


class MobileAuthSerializer(AbstractBaseAliasAuthenticationSerializer):
    @property
    def alias_type(self):
        return 'mobile'

    @property
    def alias_field_name(self):
        return api_settings.PASSWORDLESS_USER_MOBILE_FIELD_NAME

    phone_regex = RegexValidator(regex=r'^\+[1-9]\d{1,14}$',
                                 message="Mobile number must be entered in the format:"
                                         " '+999999999'. Up to 15 digits allowed.")
    mobile = serializers.CharField(validators=[phone_regex], max_length=17)
    captcha_token = serializers.CharField()


"""
Verification
"""


class AbstractBaseAliasVerificationSerializer(serializers.Serializer):
    """
    Abstract class that returns a callback token based on the field given
    Returns a token if valid, None or a message if not.
    """
    @property
    def alias_type(self):
        # The alias type, either email or mobile
        raise NotImplementedError

    def validate(self, attrs):

        msg = _('There was a problem with your request.')

        if self.alias_type:
            # Get request.user
            # Get their specified valid endpoint
            # Validate

            request = self.context["request"]
            if request and hasattr(request, "user"):
                user = request.user
                if user:
                    if not user.is_active:
                        # If valid, return attrs so we can create a token in our logic controller
                        msg = _('User account is disabled.')

                    else:
                        if hasattr(user, self.alias_type):
                            # Has the appropriate alias type
                            attrs['user'] = user
                            return attrs
                        else:
                            msg = _('This user doesn\'t have an %s.' % self.alias_type)
            raise serializers.ValidationError(msg)
        else:
            msg = _('Missing %s.') % self.alias_type
            raise serializers.ValidationError(msg)


class EmailVerificationSerializer(AbstractBaseAliasVerificationSerializer):
    @property
    def alias_type(self):
        return 'email'


class MobileVerificationSerializer(AbstractBaseAliasVerificationSerializer):
    @property
    def alias_type(self):
        return 'mobile'


"""
Callback Token
"""


def token_age_validator(value):
    """
    Check token age
    Makes sure a token is within the proper expiration datetime window.
    """
    valid_token = validate_token_age(value)
    if not valid_token:
        raise serializers.ValidationError("The token you entered isn't valid.")
    return value


class AbstractBaseCallbackTokenSerializer(serializers.Serializer):
    """
    Abstract class inspired by DRF's own token serializer.
    Returns a user if valid, None or a message if not.
    """
    phone_regex = RegexValidator(regex=r'^\+[1-9]\d{1,14}$',
                                 message="Mobile number must be entered in the format:"
                                         " '+999999999'. Up to 15 digits allowed.")

    email = serializers.EmailField(required=False)  # Needs to be required=false to require both.
    mobile = serializers.CharField(required=False, validators=[phone_regex], max_length=17)
    token = TokenField(min_length=6, max_length=6, validators=[token_age_validator])

    def validate_alias(self, attrs):
        email = attrs.get('email', None)
        mobile = attrs.get('mobile', None)

        if email and mobile:
            raise serializers.ValidationError()

        if not email and not mobile:
            raise serializers.ValidationError()

        if email:
            return api_settings.PASSWORDLESS_USER_EMAIL_FIELD_NAME, email
        elif mobile:
            return api_settings.PASSWORDLESS_USER_MOBILE_FIELD_NAME, mobile

        return None


class CallbackTokenAuthSerializer(AbstractBaseCallbackTokenSerializer):

    def validate(self, attrs):
        # Check Aliases
        try:
            alias_field_name, alias = self.validate_alias(attrs)
            callback_token = attrs.get('token', None)
            user = User.objects.get(**{alias_field_name+'__iexact': alias})
            token = CallbackToken.objects.get(**{'user': user,
                                                 'key': callback_token,
                                                 'type': CallbackToken.TOKEN_TYPE_AUTH,
                                                 'is_active': True})

            if token.user == user:
                # Check the token type for our uni-auth method.
                # authenticates and checks the expiry of the callback token.
                if not user.is_active:
                    msg = _('User account is disabled.')
                    raise serializers.ValidationError(msg)

                if api_settings.PASSWORDLESS_USER_MARK_EMAIL_VERIFIED \
                        or api_settings.PASSWORDLESS_USER_MARK_MOBILE_VERIFIED:
                    # Mark this alias as verified
                    user = User.objects.get(pk=token.user.pk)
                    success = verify_user_alias(user, token)

                    if success is False:
                        msg = _('Error validating user alias.')
                        raise serializers.ValidationError(msg)

                attrs['user'] = user
                if api_settings.PASSWORDLESS_USER_DO_LOGIN:
                  django_login(self.context["request"], user)
                return attrs

            else:
                msg = _('Invalid Token')
                raise serializers.ValidationError(msg)
        except CallbackToken.DoesNotExist:
            msg = _('Invalid alias parameters provided.')
            raise serializers.ValidationError(msg)
        except User.DoesNotExist:
            msg = _('Invalid user alias parameters provided.')
            raise serializers.ValidationError(msg)
        except ValidationError:
            msg = _('Invalid alias parameters provided.')
            raise serializers.ValidationError(msg)


class CallbackTokenVerificationSerializer(AbstractBaseCallbackTokenSerializer):
    """
    Takes a user and a token, verifies the token belongs to the user and
    validates the alias that the token was sent from.
    """

    def validate(self, attrs):
        try:
            alias_type, alias = self.validate_alias(attrs)
            user_id = self.context.get("user_id")
            user = User.objects.get(**{'id': user_id, alias_type+'__iexact': alias})
            callback_token = attrs.get('token', None)

            token = CallbackToken.objects.get(**{'user': user,
                                                 'key': callback_token,
                                                 'type': CallbackToken.TOKEN_TYPE_VERIFY,
                                                 'is_active': True})
            if token.user == user:
                # Mark this alias as verified
                success = verify_user_alias(user, token)
                if success is False:
                    logger.debug("drfpasswordless: Error verifying alias.")
                attrs['user'] = user
                return attrs
            else:
                msg = _('This token is invalid. Try again later.')
                logger.debug("drfpasswordless: User token mismatch when verifying alias.")

        except CallbackToken.DoesNotExist:
            msg = _('We could not verify this alias.')
            logger.debug("drfpasswordless: Tried to validate alias with bad token.")
            pass
        except User.DoesNotExist:
            msg = _('We could not verify this alias.')
            logger.debug("drfpasswordless: Tried to validate alias with bad user.")
            pass
        except PermissionDenied:
            msg = _('Insufficient permissions.')
            logger.debug("drfpasswordless: Permission denied while validating alias.")
            pass

        raise serializers.ValidationError(msg)


"""
Responses
"""


class TokenResponseSerializer(serializers.Serializer):
    """
    Our default response serializer.
    """
    token = serializers.CharField(source='key')
    key = serializers.CharField(write_only=True)


