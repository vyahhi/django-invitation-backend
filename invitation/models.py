import datetime
import random
from django.db import models
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.translation import ugettext_lazy as _
from django.utils.hashcompat import sha_constructor
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from app_settings import PERFORMANCE_FUNC, REWARD_THRESHOLD
from app_settings import EXPIRE_DAYS, INITIAL_INVITATIONS
import signals


def default_performance_calculator(invitation_stats):
    total = invitation_stats.available + invitation_stats.sent
    try:
        send_ratio = float(invitation_stats.sent) / total
    except ZeroDivisionError:
        send_ratio = 0.0
    try:
        accept_ratio = float(invitation_stats.accepted) / invitation_stats.sent
    except ZeroDivisionError:
        accept_ratio = 0.0
    return min((send_ratio + accept_ratio) * 0.6, 1.0)


class InvitationError(Exception):
    pass


class InvitationManager(models.Manager):
    def invite(self, user, email):
        invitation = None
        try:
            invitation = self.filter(user=user, email=email)[0]
            if not invitation.is_valid():
                invitation = None
        except (Invitation.DoesNotExist, IndexError):
            pass
        if invitation is None:
            user.invitation_stats.use()
            key = '%s%0.16f%s%s' % (settings.SECRET_KEY,
                                    random.random(),
                                    user.email,
                                    email)
            key = sha_constructor(key).hexdigest()
            invitation = self.create(user=user, email=email, key=key)
        return invitation
    invite.alters_data = True

    def find(self, invitation_key):
        try:
            invitation = self.filter(key=invitation_key)[0]
        except IndexError:
            raise Invitation.DoesNotExist
        if not invitation.is_valid():
            invitation.delete()
            raise Invitation.DoesNotExist
        return invitation

    def valid(self):
        expiration = datetime.datetime.now() - datetime.timedelta(EXPIRE_DAYS)
        return self.get_query_set().filter(date_invited__gte=expiration)


class Invitation(models.Model):
    user = models.ForeignKey(User, related_name='invitations')
    email = models.EmailField(_(u'e-mail'))
    key = models.CharField(_(u'invitation key'), max_length=40, unique=True)
    date_invited = models.DateTimeField(_(u'date invited'),
                                        default=datetime.datetime.now)

    objects = InvitationManager()

    class Meta:
        verbose_name = _(u'invitation')
        verbose_name_plural = _(u'invitations')
        ordering = ('-date_invited',)

    def __unicode__(self):
        return _('%(username)s invited %(email)s on %(date)s') % {
            'username': self.user.username,
            'email': self.email,
            'date': str(self.date_invited.date()),
        }

    @models.permalink
    def get_absolute_url(self):
        return ('invitation_register', (), {'invitation_key': self.key})

    @property
    def _expires_at(self):
        return self.date_invited + datetime.timedelta(EXPIRE_DAYS)

    def is_valid(self):
        return datetime.datetime.now() < self._expires_at

    def expiration_date(self):
        return self._expires_at.date()
    expiration_date.short_description = _(u'expiration date')
    expiration_date.admin_order_field = 'date_invited'

    def send_email(self, email=None, site=None):
        email = email or self.email
        site = site or Site.objects.get_current()
        subject = render_to_string('invitation/invitation_email_subject.txt',
                                   {'invitation': self, 'site': site})
        # Email subject *must not* contain newlines
        subject = ''.join(subject.splitlines())
        message = render_to_string('invitation/invitation_email.txt',
                                   {'invitation': self,
                                    'expiration_days': EXPIRE_DAYS,
                                    'site': site})
        send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [email])
        signals.invitation_sent.send(sender=self)

    def mark_accepted(self, new_user):
        self.user.invitation_stats.mark_accepted()
        signals.invitation_accepted.send(sender=self,
                                         inviting_user=self.user,
                                         new_user=new_user)
        self.delete()
    mark_accepted.alters_data = True


class InvitationStatsManager(models.Manager):
    def give_invitations(self, user=None, count=None):
        rewarded_users = 0
        invitations_given = 0
        if not isinstance(count, int) and not callable(count):
            raise TypeError('Count must be int or callable.')
        if user is None:
            qs = self.get_query_set()
        else:
            qs = self.filter(user=user)
        for instance in qs:
            if callable(count):
                c = count(instance.user)
            else:
                c = count
            if c:
                instance.add_available(c)
                rewarded_users += 1
                invitations_given += c
        return rewarded_users, invitations_given

    def reward(self, user=None, reward_count=INITIAL_INVITATIONS):
        def count(user):
            if user.invitation_stats.performance >= REWARD_THRESHOLD:
                return reward_count
            return 0
        return self.give_invitations(user, count)


class InvitationStats(models.Model):
    user = models.OneToOneField(User,
                                related_name='invitation_stats')
    available = models.IntegerField(_(u'available invitations'),
                                    default=INITIAL_INVITATIONS)
    sent = models.IntegerField(_(u'invitations sent'), default=0)
    accepted = models.IntegerField(_(u'invitations accepted'), default=0)

    objects = InvitationStatsManager()

    class Meta:
        verbose_name = verbose_name_plural = _(u'invitation stats')
        ordering = ('-user',)

    def __unicode__(self):
        return _(u'invitation stats for %(username)s') % {
                                               'username': self.user.username}

    @property
    def performance(self):
        if PERFORMANCE_FUNC:
            return PERFORMANCE_FUNC(self)
        return default_performance_calculator(self)

    def add_available(self, count=1):
        self.available = models.F('available') + count
        self.save()
        signals.invitation_added.send(sender=self, user=self.user, count=count)
    add_available.alters_data = True

    def use(self, count=1):
        if getattr(settings, 'INVITE_ONLY', False):
            if self.available - count >= 0:
                self.available = models.F('available') - count
            else:
                raise InvitationError('No available invitations.')
        self.sent = models.F('sent') + count
        self.save()
    use.alters_data = True

    def mark_accepted(self, count=1):
        if self.accepted + count > self.sent:
            raise InvitationError('There can\'t be more accepted ' \
                                  'invitations than sent invitations.')
        self.accepted = models.F('accepted') + count
        self.save()
    mark_accepted.alters_data = True


def create_stats(sender, instance, created, raw, **kwargs):
    if created and not raw:
        InvitationStats.objects.create(user=instance)
models.signals.post_save.connect(create_stats,
                                 sender=User,
                                 dispatch_uid='invitation.models.create_stats')
