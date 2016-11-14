"""Definition of the discussion class."""
from itertools import groupby, chain
import traceback
from datetime import datetime
from collections import defaultdict

import simplejson as json
from pyramid.security import Allow, ALL_PERMISSIONS
from pyramid.settings import asbool
from pyramid.path import DottedNameResolver
from sqlalchemy import (
    Column,
    Integer,
    UnicodeText,
    DateTime,
    Text,
    String,
    Boolean,
    event,
    ForeignKey,
    func,
    inspect,
)
from sqlalchemy.orm import (
    relationship, join, subqueryload_all, backref)
from sqlalchemy.exc import InvalidRequestError

from assembl.lib import config
from assembl.lib.utils import slugify, get_global_base_url, full_class_name
from ..lib.sqla_types import URLString
from ..lib.locale import strip_country
from . import DiscussionBoundBase
from ..lib.sqla_types import CoerceUnicode
from ..semantic.virtuoso_mapping import QuadMapPatternS
from ..auth import (
    P_READ, R_SYSADMIN, P_ADMIN_DISC, R_PARTICIPANT, P_SYSADMIN,
    CrudPermissions, Authenticated, Everyone)
from .auth import (
    DiscussionPermission, Role, Permission, User, UserRole, LocalUserRole,
    UserTemplate)
from .preferences import Preferences
from ..semantic.namespaces import (CATALYST, ASSEMBL, DCTERMS)


resolver = DottedNameResolver(__package__)


class Discussion(DiscussionBoundBase):
    """
    The context for a specific Assembl discussion.

    Most Assembl entities exist in the scope of a discussion, and inherit from
    :py:class:`assembl.models.DiscussionBoundBase`.
    """
    __tablename__ = "discussion"
    rdf_class = CATALYST.Conversation

    id = Column(Integer, primary_key=True,
                info={'rdf': QuadMapPatternS(None, ASSEMBL.db_id)})

    topic = Column(UnicodeText, nullable=False,
                   info={'rdf': QuadMapPatternS(None, DCTERMS.title)})

    slug = Column(CoerceUnicode, nullable=False, unique=True, index=True)

    creation_date = Column(DateTime, nullable=False, default=datetime.utcnow,
                           info={'rdf': QuadMapPatternS(None, DCTERMS.created)})
    objectives = Column(UnicodeText)
    instigator = Column(UnicodeText)
    introduction = Column(UnicodeText)
    introductionDetails = Column(UnicodeText)
    subscribe_to_notifications_on_signup = Column(Boolean, default=True)
    web_analytics_piwik_id_site = Column(Integer, nullable=True, default=None)
    help_url = Column(URLString, nullable=True, default=None)
    logo_url = Column(URLString, nullable=True, default=None)
    homepage_url = Column(URLString, nullable=True, default=None)
    show_help_in_debate_section = Column(Boolean, default=True)
    preferences_id = Column(Integer, ForeignKey(Preferences.id))
    creator_id = Column(Integer, ForeignKey('user.id', ondelete="SET NULL"))

    preferences = relationship(Preferences, backref=backref(
        'discussion'), cascade="all, delete-orphan", single_parent=True)
    creator = relationship('User', backref="discussions_created")

    @property
    def admin_source(self):
        """ Return the admin source for this discussion.  Used by notifications
        Very naive temporary implementation, to be revised with a proper relationship later """
        from .mail import AbstractMailbox
        for source in self.sources:
            if isinstance(source, AbstractMailbox):
                return source
        raise ValueError("No source of type AbstractMailbox found to serve as admin source")


    def check_url_or_none(self, url):
        if url == '':
            url = None
        if url is not None:
            from urlparse import urlparse
            parsed_url = urlparse(url)
            from pyramid.httpexceptions import HTTPBadRequest
            if not parsed_url.scheme:
                raise HTTPBadRequest(
                    "The homepage url does not have a scheme. Must be either http or https"
                )
    
            if parsed_url.scheme not in (u'http', u'https'):
                raise HTTPBadRequest(
                    "The url has an incorrect scheme. Only http and https are accepted for homepage url"
                )
        return url

    @property
    def homepage(self):
        return self.homepage_url

    @homepage.setter
    def homepage(self, url):
        url = self.check_url_or_none(url)
        self.homepage_url = url

    @property
    def logo(self):
        return self.logo_url

    @logo.setter
    def logo(self, url):
        url = self.check_url_or_none(url)
        self.logo_url = url

    def read_post_ids(self, user_id):
        from .post import Post
        from .action import ViewPost
        return (x[0] for x in self.db.query(Post.id).join(
            ViewPost
        ).filter(
            Post.discussion_id == self.id,
            ViewPost.actor_id == user_id,
            ViewPost.post_id == Post.id
        ))

    def get_read_posts_ids_preload(self, user_id):
        from .post import Post
        return json.dumps([
            Post.uri_generic(id) for id in self.read_post_ids(user_id)])

    def import_from_sources(self, only_new=True):
        for source in self.sources:
            # refresh after calling
            source = self.db.merge(source)
            assert source != None
            assert source.id
            try:
                source.import_content(only_new=only_new)
            except:
                traceback.print_exc()

    def __init__(self, session=None, *args, **kwargs):
        session = session or self.default_db

        kwargs['preferences'] = preferences = Preferences(
            name='discussion_'+kwargs.get('slug', str(id(self))),
            cascade_preferences=Preferences.get_default_preferences(session))
        session.add(preferences)
        session.flush()

        root_idea = kwargs.get('root_idea', None)
        table_of_contents = kwargs.get('table_of_contents', None)
        next_synthesis = kwargs.get('next_synthesis', None)
        # create unless explicitly set to None

        if root_idea is None:
            from .idea import RootIdea
            kwargs['root_idea'] = RootIdea(discussion=self)
        else:
            root_idea.discussion = self
        if table_of_contents is None:
            from .idea_graph_view import TableOfContents
            kwargs['table_of_contents'] = TableOfContents(discussion=self)
        else:
            table_of_contents.discussion = self
        if next_synthesis is None:
            from .idea_graph_view import Synthesis
            kwargs['next_synthesis'] = Synthesis(discussion=self)
        else:
            next_synthesis.discussion = self

        super(Discussion, self).__init__(*args, **kwargs)
        if root_idea is None:
            session.add(kwargs['root_idea'])
        if table_of_contents is None:
            session.add(kwargs['table_of_contents'])
        if next_synthesis is None:
            session.add(kwargs['next_synthesis'])

        participant = session.query(Role).filter_by(name=R_PARTICIPANT).one()
        participant_template = UserTemplate(
            discussion=self, for_role=participant)
        session.add(participant_template)

    def unique_query(self):
        # DiscussionBoundBase is misleading here
        return self.db.query(self.__class__).filter_by(
            slug=self.slug), True

    @property
    def settings_json(self):
        if not self.preferences:
            return Preferences.property_defaults
        return self.preferences.values_json

    def get_discussion_id(self):
        return self.id

    @classmethod
    def get_discussion_conditions(cls, discussion_id, alias_maker=None):
        return (cls.id == discussion_id,)

    def get_next_synthesis(self):
        return self.next_synthesis

    syntheses = relationship('Synthesis')

    next_synthesis = relationship('Synthesis',
        uselist=False, secondary="outerjoin(Synthesis, SynthesisPost)",
        primaryjoin="Discussion.id == Synthesis.discussion_id",
        secondaryjoin='SynthesisPost.id == None',
        viewonly=True)

    def get_last_published_synthesis(self):
        from .idea_graph_view import Synthesis
        return self.db.query(Synthesis).filter(
            Synthesis.discussion_id == self.id and
            Synthesis.published_in_post != None
        ).order_by(Synthesis.published_in_post.creation_date.desc()
                   ).first()

    def get_all_syntheses(self):
        from .idea_graph_view import Synthesis
        return self.db.query(
            Synthesis).options(
            subqueryload_all(
            'idea_assocs.idea'),
            subqueryload_all(
            'idealink_assocs.idea_link'),
            subqueryload_all(
            Synthesis.published_in_post)).filter(
            Synthesis.discussion_id == self.id).all()

    def get_permissions_by_role(self):
        roleperms = self.db.query(Role.name, Permission.name).select_from(
            DiscussionPermission).join(Role, Permission).filter(
                DiscussionPermission.discussion_id == self.id).all()
        roleperms.sort()
        byrole = groupby(roleperms, lambda (r, p): r)
        return {r: [p for (r2, p) in rps] for (r, rps) in byrole}

    def get_roles_by_permission(self):
        permroles = self.db.query(Permission.name, Role.name).select_from(
            DiscussionPermission).join(Role, Permission).filter(
                DiscussionPermission.discussion_id == self.id).all()
        permroles.sort()
        byperm = groupby(permroles, lambda (p, r): p)
        return {p: [r for (p2, r) in prs] for (p, prs) in byperm}

    def get_readers(self):
        session = self.db
        users = session.query(User).join(
            UserRole, Role, DiscussionPermission, Permission).filter(
                DiscussionPermission.discussion_id == self.id and
                Permission.name == P_READ
            ).union(self.db.query(User).join(
                LocalUserRole, Role, DiscussionPermission, Permission).filter(
                    DiscussionPermission.discussion_id == self.id and
                    LocalUserRole.discussion_id == self.id and
                    Permission.name == P_READ)).all()
        if session.query(DiscussionPermission).join(
            Role, Permission).filter(
                DiscussionPermission.discussion_id == self.id and
                Permission.name == P_READ and
                Role.name == Authenticated).first():
            pass  # add a pseudo-authenticated user???
        if session.query(DiscussionPermission).join(
            Role, Permission).filter(
                DiscussionPermission.discussion_id == self.id and
                Permission.name == P_READ and
                Role.name == Everyone).first():
            pass  # add a pseudo-anonymous user?
        return users

    def get_all_agents_preload(self, user=None):
        from assembl.views.api.agent import _get_agents_real
        return json.dumps(_get_agents_real(
            self, user.id if user else Everyone, 'partial'))

    def get_readers_preload(self):
        return json.dumps([user.generic_json('partial') for user in self.get_readers()])

    def get_ideas_preload(self, user_id):
        from assembl.views.api.idea import _get_ideas_real
        return json.dumps(_get_ideas_real(discussion=self, user_id=user_id))

    def get_idea_links(self):
        from .idea import Idea
        return Idea.get_all_idea_links(self.id)

    def get_idea_and_links(self):
        return chain(self.ideas, self.get_idea_links())

    def get_top_ideas(self):
        from .idea import Idea
        return self.db.query(Idea).filter(
            Idea.discussion_id == self.id).filter(
                ~Idea.source_links.any()).all()

    def get_related_extracts_preload(self, user_id):
        from assembl.views.api.extract import _get_extracts_real
        return json.dumps(_get_extracts_real(discussion=self, user_id=user_id))

    def get_user_permissions(self, user_id):
        from ..auth.util import get_permissions
        return get_permissions(user_id, self.id)

    def get_user_permissions_preload(self, user_id):
        return json.dumps(self.get_user_permissions(user_id))

    def get_base_url(self, require_secure=None):
        """Get the base URL of this server

        Tied to discussion so that we can support virtual hosts or
        communities in the future and access the urls when we can't rely
        on pyramid's current request (such as when celery generates
        notifications)
        Temporarily equivalent to get_global_base_url
        """
        return get_global_base_url(require_secure)

    def check_authorized_email(self, user):
        # Check if the user has a verified email from a required domain
        require_email_domain = self.preferences['require_email_domain']
        if not require_email_domain:
            return True
        for account in user.accounts:
            if not account.verified:
                continue
            email = account.email
            if not email or '@' not in email:
                continue
            email = email.split('@', 1)[-1]
            if email.lower() in require_email_domain:
                return True
        return False

    @property
    def widget_collection_url(self):
        return "/data/Discussion/%d/widgets" % (self.id,)

    # Properties as a route context
    __parent__ = None

    @property
    def __name__(self):
        return self.slug

    @property
    def __acl__(self):
        acls = [(Allow, dp.role.name, dp.permission.name) for dp in self.acls]
        acls.append((Allow, R_SYSADMIN, ALL_PERMISSIONS))
        return acls

    def __repr__(self):
        r = super(Discussion, self).__repr__()
        return r[:-1] + self.slug + ">"

    def get_notifications(self):
        for widget in self.widgets:
            for n in widget.has_notification():
                yield n

    def get_user_template(self, role_name, autocreate=False):
        template = self.db.query(UserTemplate).join(
            Role).filter(Role.name == role_name).join(
            Discussion).filter(Discussion.id == self.id).first()
        changed = False
        if autocreate and not template:
            # There is a template user per discussion.  If it doesn't exist yet
            # create it.
            from .notification import (
                NotificationCreationOrigin, NotificationSubscriptionFollowSyntheses)
            role = self.db.query(Role).filter_by(name=role_name).one()
            template = UserTemplate(for_role=role, discussion=self)
            self.db.add(template)
            subs, changed = template.get_notification_subscriptions_and_changed()
            self.db.flush()
        return template, changed

    def get_participant_template(self):
        from ..auth import R_PARTICIPANT
        return self.get_user_template(R_PARTICIPANT, True)

    def reset_notification_subscriptions_from_defaults(self, force=True):
        """Reset all notification subscriptions for this discussion"""
        from .notification import (
            NotificationSubscription, NotificationSubscriptionStatus, NotificationCreationOrigin)
        template, changed = self.get_participant_template()
        roles_subscribed = defaultdict(list)
        for template in self.user_templates:
            template_subscriptions, changed2 = template.get_notification_subscriptions_and_changed()
            changed |= changed2
            for subscription in template_subscriptions:
                if subscription.status == NotificationSubscriptionStatus.ACTIVE:
                    roles_subscribed[subscription.__class__].append(template.role_id)
        if force or changed:
            needed_classes = UserTemplate.get_applicable_notification_subscriptions_classes()
            for notif_cls in needed_classes:
                self.reset_notification_subscriptions_for(notif_cls, roles_subscribed[notif_cls])

    def reset_notification_subscriptions_for(self, notif_cls, roles_subscribed):
        from .notification import (
            NotificationSubscription, NotificationSubscriptionStatus,
            NotificationCreationOrigin)
        from .auth import AgentStatusInDiscussion
        # Make most subscriptions inactive (simpler than deciding which ones should be)
        default_ns = self.db.query(notif_cls.id
            ).join(User, notif_cls.user_id == User.id
            ).join(LocalUserRole, LocalUserRole.user_id == User.id
            ).join(AgentStatusInDiscussion,
                   AgentStatusInDiscussion.profile_id == User.id
            ).filter(
                LocalUserRole.discussion_id == self.id,
                AgentStatusInDiscussion.discussion_id == self.id,
                AgentStatusInDiscussion.last_visit != None,
                notif_cls.discussion_id == self.id,
                notif_cls.creation_origin == NotificationCreationOrigin.DISCUSSION_DEFAULT)
        deactivated = default_ns.filter(
            notif_cls.status == NotificationSubscriptionStatus.ACTIVE)
        if roles_subscribed:
            # Make some subscriptions active (back)
            activated = default_ns.filter(
                    LocalUserRole.role_id.in_(roles_subscribed),
                    notif_cls.status == NotificationSubscriptionStatus.INACTIVE_DFT)
            self.db.query(notif_cls
                ).filter(notif_cls.id.in_(activated.subquery())
                ).update(
                    {"status": NotificationSubscriptionStatus.ACTIVE,
                     "last_status_change_date": datetime.utcnow()},
                    synchronize_session=False)
            # Materialize missing subscriptions
            missing_subscriptions = self.db.query(User.id
                ).join(LocalUserRole, LocalUserRole.user_id == User.id
                ).join(AgentStatusInDiscussion,
                       AgentStatusInDiscussion.profile_id == User.id
                ).outerjoin(notif_cls, (notif_cls.user_id == User.id) & (
                                        notif_cls.discussion_id == self.id)
                ).filter(LocalUserRole.discussion_id == self.id,
                         AgentStatusInDiscussion.discussion_id == self.id,
                         AgentStatusInDiscussion.last_visit != None,
                         LocalUserRole.role_id.in_(roles_subscribed),
                         notif_cls.id == None).distinct().all()
            for (user_id,) in missing_subscriptions:
                self.db.add(notif_cls(
                    discussion_id=self.id,
                    user_id=user_id,
                    creation_origin=NotificationCreationOrigin.DISCUSSION_DEFAULT,
                    status=NotificationSubscriptionStatus.ACTIVE))
            # exclude from deactivated query
            deactivated = deactivated.except_(
                default_ns.filter(
                    LocalUserRole.role_id.in_(roles_subscribed)))
        self.db.query(notif_cls
            ).filter(notif_cls.id.in_(deactivated.subquery())
            ).update(
                {"status": NotificationSubscriptionStatus.INACTIVE_DFT,
                 "last_status_change_date": datetime.utcnow()},
                synchronize_session=False)

        # Should we send them to the socket? We do not at this point.
        # changed = deactivated_ids + activated_ids
        # changed = self.db.query(NotificationSubscription).filter(
        #     NotificationSubscription.id.in_(changed))
        # for ns in changed:
        #     ns.send_to_changes(discussion_id=self.id)

    @classmethod
    def extra_collections(cls):
        from assembl.views.traversal import (
            CollectionDefinition, AbstractCollectionDefinition)
        from .notification import NotificationSubscription
        from ..views.traversal import (
            UserNsDictCollection, PreferenceCollection)

        class AllUsersCollection(AbstractCollectionDefinition):
            def __init__(self, cls):
                super(AllUsersCollection, self).__init__(cls, User)

            def decorate_query(self, query, owner_alias, last_alias, parent_instance, ctx):
                try:
                    return query.join(owner_alias)
                except InvalidRequestError:
                    # No real outerjoin in sqlalchemy. Use a dummy condition.
                    return query.outerjoin(
                        owner_alias, owner_alias.id != None)

            def decorate_instance(
                    self, instance, parent_instance, assocs, user_id,
                    ctx, kwargs):
                if isinstance(instance, NotificationSubscription):
                    NotificationSubscription.discussion_id = parent_instance.id

            def contains(self, parent_instance, instance):
                return True

            def get_instance(self, key, parent_instance):
                if key == 'current':
                    from pyramid.threadlocal import get_current_request
                    from pyramid.httpexceptions import HTTPUnauthorized
                    request = get_current_request()
                    if request is not None:
                        key = request.authenticated_userid
                        if key is None:
                            raise HTTPUnauthorized()
                    else:
                        raise RuntimeError()
                return super(AllUsersCollection, self).get_instance(
                    key, parent_instance)

        class ActiveWidgetsCollection(CollectionDefinition):

            def __init__(self, cls):
                super(ActiveWidgetsCollection, self).__init__(
                    cls, Discussion.widgets)

            def decorate_query(self, query, owner_alias, last_alias, parent_instance, ctx):
                from .widgets import Widget
                query = super(ActiveWidgetsCollection, self).decorate_query(
                    query, owner_alias, last_alias, parent_instance, ctx)
                query = Widget.filter_active(query)
                return query

            def contains(self, parent_instance, instance):
                return instance.is_active() and super(
                    ActiveWidgetsCollection, self).contains(
                    parent_instance, instance)

        class SourcesCollection(CollectionDefinition):
            def __init__(self, cls):
                super(SourcesCollection, self).__init__(
                    cls, cls.sources)

            def decorate_instance(
                    self, instance, parent_instance, assocs, user_id, ctx,
                    kwargs):

                super(SourcesCollection, self).decorate_instance(
                    instance, parent_instance, assocs, user_id, ctx, kwargs)

                from .generic import Content, ContentSourceIDs
                from .facebook_integration import FacebookGenericSource

                for inst in assocs[:]:
                    if isinstance(inst, FacebookGenericSource):
                        if 'is_content_sink' in kwargs:
                            is_sink = kwargs.get('is_content_sink', None)
                            data = kwargs.get('sink_data', None)
                            if is_sink:
                                if not data:
                                    raise ValueError("User must pass sink data")
                                post_id = data.get('post_id', None)
                                fb_post_id = data.get('facebook_post_id', None)
                                source = instance
                                if not post_id:
                                    raise ValueError(
                                        "Could not create content because of "
                                        "improper data input")
                                else:
                                    try:
                                        post_object = Content.\
                                            get_instance(post_id)
                                        cs = ContentSourceIDs(source=source,
                                            post=post_object,
                                            message_id_in_source=fb_post_id)
                                        assocs.append(cs)
                                    except:
                                        raise ValueError("Failed on content sink transaction")

        return {'all_users': AllUsersCollection(cls),
                'active_widgets': ActiveWidgetsCollection(cls),
                'sources': SourcesCollection(cls),
                'user_ns_kv': UserNsDictCollection(cls),
                'settings': PreferenceCollection(cls)}

    all_participants = relationship(
        User, viewonly=True, secondary=LocalUserRole.__table__,
        primaryjoin="LocalUserRole.discussion_id == Discussion.id",
        secondaryjoin=((LocalUserRole.user_id == User.id)
            & (LocalUserRole.requested == False)),
        backref="involved_in_discussion")

    #The list of praticipants actually subscribed to the discussion
    simple_participants = relationship(
        User, viewonly=True,
        secondary=join(LocalUserRole, Role,
            ((LocalUserRole.role_id == Role.id) & (Role.name == R_PARTICIPANT))),
        primaryjoin="LocalUserRole.discussion_id == Discussion.id",
        secondaryjoin=((LocalUserRole.user_id == User.id)
            & (LocalUserRole.requested == False)),
        backref="participant_in_discussion")

    def get_participants_query(self, ids_only=False, include_readers=False):
        from .auth import AgentProfile, LocalUserRole
        from .generic import Content
        from .post import Post
        from .action import ViewPost
        from .idea_content_link import Extract

        query = self.db.query(AgentProfile.id).join(LocalUserRole,
                LocalUserRole.user_id==AgentProfile.id).filter(
            LocalUserRole.discussion_id == self.id).union(
            self.db.query(AgentProfile.id).join(Post,
                Post.creator_id==AgentProfile.id).filter(
            Post.discussion_id == self.id)).union(
            self.db.query(AgentProfile.id).join(
                Extract, Extract.creator_id==AgentProfile.id).filter(
            Extract.discussion_id == self.id))
        query = query.union(self.db.query(AgentProfile.id).join(UserRole,
                UserRole.user_id==AgentProfile.id))
        if include_readers:
            query = query.union(
                self.db.query(ViewPost.actor_id).join(
                Content, Content.id==ViewPost.post_id).filter(
                Content.discussion_id==self.id))
        query = query.distinct()
        if ids_only:
            return query
        return self.db.query(AgentProfile).filter(AgentProfile.id.in_(query))

    def get_participants(self, ids_only=False):
        query = self.get_participants_query(ids_only)
        if ids_only:
            return (id for (id,) in query.all())
        return query.all()

    def get_url(self):
        from assembl.lib.frontend_urls import FrontendUrls
        frontendUrls = FrontendUrls(self)
        return frontendUrls.get_discussion_url()

    def count_contributions_per_agent(
            self, start_date=None, end_date=None, as_agent=True):
        from .post import Post
        from .auth import AgentProfile
        query = self.db.query(
            func.count(Post.id), Post.creator_id).filter(
                Post.discussion_id==self.id,
                Post.tombstone_condition())
        if start_date:
            query = query.filter(Post.creation_date >= start_date)
        if end_date:
            query = query.filter(Post.creation_date < end_date)
        query = query.group_by(Post.creator_id)
        results = query.all()
        # from highest to lowest
        results.sort(reverse=True)
        if not as_agent:
            return [(id, count) for (count, id) in results]
        agent_ids = [ag for (c, ag) in results]
        agents = self.db.query(AgentProfile).filter(
            AgentProfile.id.in_(agent_ids))
        agents_by_id = {ag.id: ag for ag in agents}
        return [(agents_by_id[id], count) for (count, id) in results]

    def count_new_visitors(
            self, start_date=None, end_date=None, as_agent=True):
        from .auth import AgentStatusInDiscussion
        query = self.db.query(
            func.count(AgentStatusInDiscussion.id)).filter_by(
            discussion_id=self.id)
        if start_date:
            query = query.filter(
                AgentStatusInDiscussion.first_visit >= start_date)
        if end_date:
            query = query.filter(
                AgentStatusInDiscussion.first_visit < end_date)
        return query.first()[0]

    def count_post_viewers(
            self, start_date=None, end_date=None, as_agent=True):
        from .post import Post
        from .action import ViewPost
        from sqlalchemy.sql.expression import distinct
        query = self.db.query(
            func.count(distinct(ViewPost.actor_id))).join(Post).filter(
                Post.discussion_id == self.id)
        if start_date:
            query = query.filter(ViewPost.creation_date >= start_date)
        if end_date:
            query = query.filter(ViewPost.creation_date < end_date)
        return query.first()[0]

    def as_mind_map(self):
        import pygraphviz
        from colors import hsv
        from datetime import datetime
        from assembl.models import Idea, IdeaLink, RootIdea
        ideas = self.db.query(Idea).filter_by(
            tombstone_date=None, discussion_id=self.id).all()
        links = self.db.query(IdeaLink).filter_by(
            tombstone_date=None).join(Idea, IdeaLink.source_id==Idea.id).filter(
            Idea.discussion_id==self.id).all()
        G = pygraphviz.AGraph()
        G.graph_attr['overlap']='prism'
        G.node_attr['penwidth']=0
        G.node_attr['shape']='rect'
        G.node_attr['style']='filled'
        G.node_attr['fillcolor'] = '#efefef'
        start_time = min((idea.creation_date for idea in ideas))
        end_time = max((idea.last_modified for idea in ideas))
        end_time = min(datetime.now(), end_time + (end_time - start_time))

        root_id = self.root_idea.id
        parent_ids = {l.target_id: l.source_id for l in links}

        def node_level(node_id):
            if node_id == root_id:
                return 0
            return 1 + node_level(parent_ids[node_id])

        for idea in ideas:
            if isinstance(idea, RootIdea):
                root_id = idea.id
                G.add_node(idea.id, label="", style="invis")
            else:
                level = node_level(idea.id)
                age = (end_time - idea.last_modified).total_seconds() / (end_time - start_time).total_seconds()
                print idea.id, start_time, idea.last_modified, end_time
                print (end_time - idea.last_modified).total_seconds(), (end_time - start_time).total_seconds()
                #empirical
                color = hsv(180-(135.0 * age), 0.15, 0.85)
                G.add_node(idea.id,
                    label=idea.short_title or "",
                    fontsize = 18 - (1.5 * level),
                    height=(20-(1.5*level))/72.0,
                    fillcolor="#%s" % color.hex)
        for link in links:
            if link.source_id == root_id:
                G.add_edge(link.source_id, link.target_id, style="invis")
            else:
                G.add_edge(link.source_id, link.target_id)
        return G

    crud_permissions = CrudPermissions(
        P_SYSADMIN, P_READ, P_ADMIN_DISC, P_SYSADMIN)

    @property
    def discussion_locales(self):
        # Ordered list, not empty.
        # TODO: Guard. Each locale should be 2-letter or posix.
        # Waiting for utility function.
        locales = self.preferences['preferred_locales']
        if locales:
            return locales
        # Use installation settings otherwise.
        return [strip_country(l) for l in config.get_config().get(
            'available_languages', 'fr en').split()]

    @discussion_locales.setter
    def discussion_locales(self, locale_list):
        # TODO: Guard.
        self.preferences['preferred_locales'] = locale_list

    # class cache, indexed by discussion id
    _discussion_services = {}

    @property
    def translation_service_class(self):
        return self.preferences["translation_service"]

    def translation_service(self):
        service_class = self.translation_service_class
        service = self._discussion_services.get(self.id, None)
        if service and not service_class:
            del self._discussion_services[self.id]
            return None
        if (service_class and service
                and full_class_name(service) != service_class):
            del self._discussion_services[self.id]
            service = None
        elif (service_class and not service
                and self.id in self._discussion_services):
            del self._discussion_services[self.id]
        if self.id not in self._discussion_services:
            try:
                if service_class:
                    service = resolver.resolve(service_class)(self)
                self._discussion_services[self.id] = service
            except:
                pass
        return self._discussion_services.get(self.id, None)

    def remove_translations(self):
        # For testing purposes
        for post in self.posts:
            post.remove_translations()

    @property
    def main_locale(self):
        return self.discussion_locales[0]

    def compose_external_uri(self, *args, **kwargs):
        """
        :use_api2 - uses API2 URL path
        pass as many nodes you want in the args
        """
        composer = ""
        base = self.get_base_url()
        if kwargs.get('use_api2', True):
            base += "/data/"
        else:
            base += "/"
        uri = self.uri(base_uri=base)
        composer += uri
        for arg in args:
            if arg:
                composer += "/%s" % arg
        return composer


def slugify_topic_if_slug_is_empty(discussion, topic, oldvalue, initiator):
    """
    if the target doesn't have a slug, slugify the topic and use that.
    """
    if not discussion.slug:
        discussion.slug = slugify(topic)


event.listen(Discussion.topic, 'set', slugify_topic_if_slug_is_empty)
