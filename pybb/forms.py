
import re
import inspect

import django
from django import forms
from django.core.exceptions import FieldError, PermissionDenied
from django.forms.models import inlineformset_factory, BaseInlineFormSet
from django.utils.decorators import method_decorator
from django.utils.text import Truncator
from django.utils.translation import gettext, gettext_lazy
from django.utils.timezone import now as tznow
from django.utils.translation import gettext as _

from pybb import compat, defaults, util, permissions
from pybb.models import Topic, Post, Attachment, PollAnswer, \
    ForumSubscription, Category, Forum, create_or_check_slug


User = compat.get_user_model()
username_field = compat.get_username_field()


class AttachmentForm(forms.ModelForm):
    class Meta(object):
        model = Attachment
        fields = ('file', )

    def clean_file(self):
        if self.cleaned_data['file'].size > defaults.PYBB_ATTACHMENT_SIZE_LIMIT:
            raise forms.ValidationError(gettext('Attachment is too big'))
        return self.cleaned_data['file']

AttachmentFormSet = inlineformset_factory(Post, Attachment, extra=1, form=AttachmentForm)


class PollAnswerForm(forms.ModelForm):
    class Meta:
        model = PollAnswer
        fields = ('text', )


class BasePollAnswerFormset(BaseInlineFormSet):
    def clean(self):
        forms_cnt = (len(self.initial_forms) + len([form for form in self.extra_forms if form.has_changed()]) -
                     len(self.deleted_forms))
        if forms_cnt > defaults.PYBB_POLL_MAX_ANSWERS:
            raise forms.ValidationError(
                gettext('You can''t add more than %s answers for poll' % defaults.PYBB_POLL_MAX_ANSWERS))
        if forms_cnt < 2:
            raise forms.ValidationError(gettext('Add two or more answers for this poll'))


PollAnswerFormSet = inlineformset_factory(Topic, PollAnswer, extra=2, max_num=defaults.PYBB_POLL_MAX_ANSWERS,
                                          form=PollAnswerForm, formset=BasePollAnswerFormset)


class PostForm(forms.ModelForm):
    class Meta:
        model = Post
        fields = ['body']  # Seulement 'body' est attendu dans les données POST
        widgets = {
            'body': util.get_markup_engine().get_widget_cls(),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        self.ip = kwargs.pop('ip', None)
        self.topic = kwargs.pop('topic', None)
        self.forum = kwargs.pop('forum', None)
        self.may_create_poll = kwargs.pop('may_create_poll', False)
        self.may_edit_topic_slug = kwargs.pop('may_edit_topic_slug', False)
        super().__init__(*args, **kwargs)

        # Ne pas ajouter de champs dynamiques sauf si explicitement requis
        if self.forum and self.may_edit_topic_slug:
            self.fields['name'] = forms.CharField(required=True, label=_('Subject'))
            self.fields['slug'] = forms.CharField(required=False, label=_('Topic slug'))
        elif self.topic and self.instance.pk and self.instance.topic.head == self.instance and self.may_edit_topic_slug:
            self.fields['name'] = forms.CharField(required=True, initial=self.topic.name, label=_('Subject'))
            self.fields['slug'] = forms.CharField(required=False, initial=self.topic.slug, label=_('Topic slug'))
        if self.may_create_poll:
            self.fields['poll_type'] = forms.TypedChoiceField(
                label=_('Poll type'),
                choices=Topic.POLL_TYPE_CHOICES,
                coerce=int,
                initial=Topic.POLL_TYPE_NONE,
                required=False
            )
            self.fields['poll_question'] = forms.CharField(
                label=_('Poll question'),
                required=False,
                widget=forms.Textarea(attrs={'class': 'no-markitup'})
            )

        self.available_smiles = defaults.PYBB_SMILES
        self.smiles_prefix = defaults.PYBB_SMILES_PREFIX

    def clean_body(self):
        body = self.cleaned_data['body']
        user = self.user or self.instance.user
        if defaults.PYBB_BODY_VALIDATOR:
            defaults.PYBB_BODY_VALIDATOR(user, body)
        for cleaner in defaults.PYBB_BODY_CLEANERS:
            body = util.get_body_cleaner(cleaner)(user, body)
        return body

    def clean(self):
        cleaned_data = super().clean()
        # Ne pas valider poll_type/poll_question si may_create_poll est False
        if self.may_create_poll:
            poll_type = cleaned_data.get('poll_type', Topic.POLL_TYPE_NONE)
            poll_question = cleaned_data.get('poll_question', None)
            if poll_type != Topic.POLL_TYPE_NONE and not poll_question:
                raise forms.ValidationError(_('Poll question is required when adding a poll'))
        return cleaned_data

    def save(self, commit=True):
        if self.instance.pk:
            # Cas d'édition d'un post existant
            post = super().save(commit=False)
            if self.user:
                post.user = self.user
            if post.topic.head == post and self.may_edit_topic_slug:
                post.topic.name = self.cleaned_data.get('name', post.topic.name)
                if self.may_create_poll:
                    post.topic.poll_type = self.cleaned_data.get('poll_type', post.topic.poll_type)
                    post.topic.poll_question = self.cleaned_data.get('poll_question', post.topic.poll_question)
                post.topic.updated = tznow()
                if commit:
                    post.topic.save()
            post.updated = tznow()
            if commit:
                post.save()
            return post, post.topic

        # Cas de création d’un nouveau post
        allow_post = True
        if defaults.PYBB_PREMODERATION:
            allow_post = defaults.PYBB_PREMODERATION(self.user, self.cleaned_data['body'])

        if self.forum:
            topic = Topic(
                forum=self.forum,
                user=self.user,
                name=self.cleaned_data.get('name', 'New Topic'),
                poll_type=self.cleaned_data.get('poll_type', Topic.POLL_TYPE_NONE) if self.may_create_poll else Topic.POLL_TYPE_NONE,
                poll_question=self.cleaned_data.get('poll_question', None) if self.may_create_poll else None,
                slug=self.cleaned_data.get('slug', None) if self.may_edit_topic_slug else None,
            )
            if not allow_post:
                topic.on_moderation = True
        elif self.topic:
            topic = self.topic
        else:
            raise ValueError('Topic or forum must be provided')

        post = Post(
            user=self.user,
            body=self.cleaned_data['body'],
            user_ip=self.ip if self.ip else '0.0.0.0'
        )
        if not allow_post:
            post.on_moderation = True

        if commit:
            topic.save()
            post.topic = topic
            post.save()
        return post, topic


class MovePostForm(forms.Form):

    def __init__(self, instance, user, *args, **kwargs):
        super(MovePostForm, self).__init__(*args, **kwargs)
        self.instance = instance
        self.user = user
        self.post = self.instance
        self.category, self.forum, self.topic = self.post.get_parents()

        if not self.post.is_topic_head:
            # we do not move an entire topic but a part of it's posts. Let's select those posts.
            self.posts_to_move = Post.objects.filter(created__gte=self.post.created,
                                                     topic=self.topic).order_by('created', 'pk')
            # if multiple posts exists with the same created datetime, it's important to keep the
            # same order and do not move some posts which could be "before" our post.
            # We can not just filter by adding `pk__gt=self.post.pk` because we could exclude
            # some posts if for some reasons, a lesser pk has a greater "created" datetime
            # Most of the time, we just do one extra request to be sure the first post is
            # the wanted one
            first_pk = self.posts_to_move.values_list('pk', flat=True)[0]
            while first_pk != self.post.pk:
                self.posts_to_move = self.posts_to_move.exclude(pk=first_pk)
                first_pk = self.posts_to_move.values_list('pk', flat=True)[0]

            i = 0
            choices = []
            for post in self.posts_to_move[1:]:  # all except the current one
                i += 1
                bvars = {'author': util.get_pybb_profile(post.user).get_display_name(),
                         'abstract': Truncator(post.body_text).words(8),
                         'i': i}
                label = _('%(i)d (%(author)s: "%(abstract)s")') % bvars
                choices.append((i, label))
            choices.insert(0, (0, _('None')))
            choices.insert(0, (-1, _('All')))
            self.fields['number'] = forms.TypedChoiceField(
                label=gettext_lazy('Number of following posts to move with'),
                choices=choices, required=True, coerce=int,
            )
            # we move the entire topic, so we want to change it's forum.
            # So, let's exclude the current one

        # get all forum where we can move this post (and the others)
        move_to_forums = permissions.perms.filter_forums(self.user, Forum.objects.all())
        if self.post.is_topic_head:
            # we move the entire topic, so we want to change it's forum.
            # So, let's exclude the current one
            move_to_forums = move_to_forums.exclude(pk=self.forum.pk)
        last_cat_pk = None
        choices = []
        for forum in move_to_forums.order_by('category__position', 'position', 'name'):
            if not permissions.perms.may_create_topic(self.user, forum):
                continue
            if last_cat_pk != forum.category.pk:
                last_cat_pk = forum.category.pk
                choices.append(('%s' % forum.category, []))
            if self.forum.pk == forum.pk:
                name = '%(forum)s (forum of the current post)' % {'forum': self.forum}
            else:
                name = '%s' % forum
            choices[-1][1].append((forum.pk, name))

        self.fields['move_to'] = forms.ChoiceField(label=gettext_lazy('Move to forum'),
                                                   initial=self.forum.pk,
                                                   choices=choices, required=True,)
        self.fields['name'] = forms.CharField(label=_('New subject'),
                                              initial=self.topic.name,
                                              max_length=255, required=True)
        if permissions.perms.may_edit_topic_slug(self.user):
            self.fields['slug'] = forms.CharField(label=_('New topic slug'),
                                                  initial=self.topic.slug,
                                                  max_length=255, required=False)

    def get_new_topic(self):
        if hasattr(self, '_new_topic'):
            return self._new_topic
        if self.post.is_topic_head:
            topic = self.topic
        else:
            topic = Topic(user=self.post.user)

        if topic.name != self.cleaned_data['name']:
            topic.name = self.cleaned_data['name']
            # force slug auto-rebuild if slug is not speficied and topic is renamed
            topic.slug = self.cleaned_data.get('slug', None)
        elif self.cleaned_data.get('slug', None):
            topic.slug = self.cleaned_data['slug']

        topic.forum = Forum.objects.get(pk=self.cleaned_data['move_to'])
        topic.slug = create_or_check_slug(topic, Topic, forum=topic.forum)
        topic.save()
        return topic

    @method_decorator(compat.get_atomic_func())
    def save(self):
        data = self.cleaned_data
        topic = self.get_new_topic()

        if not self.post.is_topic_head:
            # we move some posts
            posts = self.posts_to_move
            if data['number'] != -1:
                number = data['number'] + 1  # we want to move at least the current post ;-)
                posts = posts[0:number]
            # update posts
            # we can not update with subqueries on same table with mysql 5.5
            # it raises: You can't specify target table 'pybb_post' for update in FROM clause
            # so we need to get all pks... It's bad for perfs, but posts are not often splitted...
            posts_pks = [p.pk for p in posts]
            Post.objects.filter(pk__in=posts_pks).update(topic_id=topic.pk)

        topic.update_counters()
        topic.forum.update_counters()

        if topic.pk != self.topic.pk:
            # we just created a new topic. let's update the counters
            self.topic.update_counters()
        if self.forum.pk != topic.forum.pk:
            self.forum.update_counters()
        return Post.objects.get(pk=self.post.pk)


class AdminPostForm(PostForm):
    """
    Superusers can post messages from any user and from any time
    If no user with specified name - new user will be created
    """
    login = forms.CharField(label=gettext_lazy('User'))

    def __init__(self, *args, **kwargs):
        if args:
            kwargs.update(dict(zip(inspect.getargspec(forms.ModelForm.__init__)[0][1:], args)))
        if 'instance' in kwargs and kwargs['instance']:
            kwargs.setdefault('initial', {}).update({'login': getattr(kwargs['instance'].user, username_field)})
        super(AdminPostForm, self).__init__(**kwargs)

    def save(self, *args, **kwargs):
        try:
            self.user = User.objects.filter(**{username_field: self.cleaned_data['login']}).get()
        except User.DoesNotExist:
            if username_field != 'email':
                create_data = {username_field: self.cleaned_data['login'],
                               'email': '%s@example.com' % self.cleaned_data['login'],
                               'is_staff': False}
            else:
                create_data = {'email': '%s@example.com' % self.cleaned_data['login'],
                               'is_staff': False}
            self.user = User.objects.create(**create_data)
        return super(AdminPostForm, self).save(*args, **kwargs)


try:
    class EditProfileForm(forms.ModelForm):
        class Meta(object):
            model = util.get_pybb_profile_model()
            fields = ['signature', 'time_zone', 'language', 'show_signatures', 'avatar']

        def __init__(self, *args, **kwargs):
            super(EditProfileForm, self).__init__(*args, **kwargs)
            self.fields['signature'].widget = forms.Textarea(attrs={'rows': 2, 'cols:': 60})

        def clean_avatar(self):
            if self.cleaned_data['avatar'] and (self.cleaned_data['avatar'].size > defaults.PYBB_MAX_AVATAR_SIZE):
                forms.ValidationError(gettext('Avatar is too large, max size: %s bytes' %
                                               defaults.PYBB_MAX_AVATAR_SIZE))
            return self.cleaned_data['avatar']

        def clean_signature(self):
            value = self.cleaned_data['signature'].strip()
            if len(re.findall(r'\n', value)) > defaults.PYBB_SIGNATURE_MAX_LINES:
                raise forms.ValidationError('Number of lines is limited to %d' % defaults.PYBB_SIGNATURE_MAX_LINES)
            if len(value) > defaults.PYBB_SIGNATURE_MAX_LENGTH:
                raise forms.ValidationError('Length of signature is limited to %d' % defaults.PYBB_SIGNATURE_MAX_LENGTH)
            return value
except FieldError:
    pass


class UserSearchForm(forms.Form):
    query = forms.CharField(required=False, label='')

    def filter(self, qs):
        if self.is_valid():
            query = self.cleaned_data['query']
            return qs.filter(**{'%s__contains' % username_field: query})
        else:
            return qs


class PollForm(forms.Form):
    def __init__(self, topic, *args, **kwargs):
        self.topic = topic

        super(PollForm, self).__init__(*args, **kwargs)

        qs = PollAnswer.objects.filter(topic=topic)
        if topic.poll_type == Topic.POLL_TYPE_SINGLE:
            self.fields['answers'] = forms.ModelChoiceField(
                label='', queryset=qs, empty_label=None,
                widget=forms.RadioSelect())
        elif topic.poll_type == Topic.POLL_TYPE_MULTIPLE:
            self.fields['answers'] = forms.ModelMultipleChoiceField(
                label='', queryset=qs,
                widget=forms.CheckboxSelectMultiple())

    def clean_answers(self):
        answers = self.cleaned_data['answers']
        if self.topic.poll_type == Topic.POLL_TYPE_SINGLE:
            return [answers]
        else:
            return answers


class ForumSubscriptionForm(forms.Form):
    def __init__(self, user, forum, instance=None, *args, **kwargs):
        super(ForumSubscriptionForm, self).__init__(*args, **kwargs)
        self.user = user
        self.forum = forum
        self.instance = instance

        type_choices = list(ForumSubscription.TYPE_CHOICES)
        if instance :
            type_choices.append(
                ('unsubscribe', _('be unsubscribe from this forum')))
            type_initial = instance.type
        else:
            type_initial = ForumSubscription.TYPE_NOTIFY
        self.fields['type'] = forms.ChoiceField(
            label=_('You want to'), choices=type_choices, initial=type_initial,
            widget=forms.RadioSelect())

        topic_choices = (
            ('new', _('only new topics')),
            ('all', _('all topics of the forum')),
        )
        self.fields['topics'] = forms.ChoiceField(
            label=_('Concerned topics'), choices=topic_choices,
            initial=topic_choices[0][0], widget=forms.RadioSelect())

    def process(self):
        """
        saves or deletes the ForumSubscription's instance
        """
        action = self.cleaned_data.get('type')
        all_topics = self.cleaned_data.get('topics') == 'all'
        if action == 'unsubscribe':
            self.instance.delete(all_topics=all_topics)
            return 'delete-all' if all_topics else 'delete'
        else:
            if not self.instance:
                self.instance = ForumSubscription()
                self.instance.user = self.user
                self.instance.forum = self.forum
            self.instance.type = int(self.cleaned_data.get('type'))
            self.instance.save(all_topics=all_topics)
            return 'subscribe-all' if all_topics else 'subscribe'


class ModeratorForm(forms.Form):

    def __init__(self, user, *args, **kwargs):

        """
        Creates the form to grant moderator privileges, checking if the request user has the
        permission to do so.

        :param user: request user
        """

        super(ModeratorForm, self).__init__(*args, **kwargs)
        categories = Category.objects.all()
        self.authorized_forums = []
        if not permissions.perms.may_manage_moderators(user):
            raise PermissionDenied()
        for category in categories:
            forums = [forum.pk for forum in category.forums.all() if permissions.perms.may_change_forum(user, forum)]
            if forums:
                self.authorized_forums += forums
                self.fields['cat_%d' % category.pk] = forms.ModelMultipleChoiceField(
                    label=category.name,
                    queryset=category.forums.filter(pk__in=forums),
                    widget=forms.CheckboxSelectMultiple(),
                    required=False
                )

    def process(self, target_user):
        """
        Updates the target user moderator privileges

        :param target_user: user to update
        """

        cleaned_forums = self.cleaned_data.values()
        initial_forum_set = target_user.forum_set.all()
        # concatenation of the lists into one
        checked_forums = [forum for queryset in cleaned_forums for forum in queryset]
        # keep all the forums, the request user does't have the permission to change
        untouchable_forums = [forum for forum in initial_forum_set if forum.pk not in self.authorized_forums]
        if django.VERSION < (1, 9):
            target_user.forum_set = checked_forums + untouchable_forums
        else:
            target_user.forum_set.set(checked_forums + untouchable_forums)
