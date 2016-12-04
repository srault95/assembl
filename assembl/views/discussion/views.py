"""The basic views that host the one-page app"""
import json
import os.path

from pyramid.view import view_config
from pyramid.response import Response
from pyramid.renderers import render_to_response
from pyramid.security import authenticated_userid, Everyone
from pyramid.httpexceptions import (
    HTTPNotFound, HTTPSeeOther, HTTPUnauthorized)
from pyramid.i18n import TranslationStringFactory
from sqlalchemy.orm.exc import NoResultFound

from ...lib.utils import path_qs
from ...lib.frontend_urls import FrontendUrls
from ...auth import P_READ, P_ADD_EXTRACT
from ...auth.util import user_has_permission
from ...models import (
    Discussion,
    User,
    Role,
    Post,
    Idea,
    Locale,
)
from .. import (
    HTTPTemporaryRedirect, get_default_context as base_default_context,
    get_locale_from_request)
from ...nlp.translation_service import DummyGoogleTranslationService
from ..auth.views import get_social_autologin


FIXTURE = os.path.join(os.path.dirname(__file__),
                       '../../static/js/fixtures/nodes.json')

_ = TranslationStringFactory('assembl')

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'templates')


def get_default_context(request):
    base = base_default_context(request)
    slug = request.matchdict['discussion_slug']
    try:
        discussion = Discussion.default_db.query(Discussion).filter(Discussion.slug==slug).one()
    except NoResultFound:
        raise HTTPNotFound(_("No discussion found for slug=%s") % slug)
    return dict(base, discussion=discussion)


def get_styleguide_components():
    """ get all .jinja2 files from templates/styleguide directory """
    views_path = os.path.join(TEMPLATE_PATH, 'styleguide', 'components')
    views = {}

    for (dirpath, dirname, filenames) in os.walk(views_path):
        for filename in filenames:
            if filename.endswith('.jinja2') and filename != 'index.jinja2':
                view_path = os.path.join('styleguide', 'components', filename)
                view_name = filename.split('.')[0].replace('_', ' ')
                views[view_name] = view_path

    return views


@view_config(route_name='home', request_method='GET', http_cache=60)
def home_view(request):
    """The main view on a discussion"""
    user_id = authenticated_userid(request) or Everyone
    context = get_default_context(request)
    discussion = context["discussion"]
    canRead = user_has_permission(discussion.id, user_id, P_READ)
    if not canRead and user_id == Everyone:
        # User isn't logged-in and discussion isn't public:
        # redirect to login page
        # need to pass the route to go to *after* login as well

        # With regards to a next_view, if explicitly stated, then
        # that is the next view. If not stated, the referer takes
        # precedence. In case of failure, login redirects to the
        # discussion which is its context.
        next_view = request.params.get('next', None)
        if not next_view and discussion:
            # If referred here from a post url, want to be able to
            # send the user back. Usually, Assembl will send the user
            # here to login on private discussions.
            referrer = request.url
            next_view = path_qs(referrer)

        login_url = get_social_autologin(request, discussion, next_view)
        if login_url:
            pass
        elif next_view:
            login_url = request.route_url("contextual_login",
                                          discussion_slug=discussion.slug,
                                          _query={"next": next_view})
        else:
            login_url = request.route_url(
                'contextual_login', discussion_slug=discussion.slug)
        return HTTPTemporaryRedirect(login_url)
    elif not canRead:
        # User is logged-in but doesn't have access to the discussion
        # Would use render_to_response, except for the 401
        from pyramid_jinja2 import IJinja2Environment
        jinja_env = request.registry.queryUtility(
            IJinja2Environment, name='.jinja2')
        template = jinja_env.get_template('cannot_read_discussion.jinja2')
        body = template.render(get_default_context(request))
        return Response(body, 401)

    # if the route asks for a post, get post content (because this is needed for meta tags)
    route_name = request.matched_route.name
    if route_name == "purl_posts":
        post_id = FrontendUrls.getRequestedPostId(request)
        if not post_id:
            return HTTPSeeOther(request.route_url(
                'home', discussion_slug=discussion.slug))
        post = Post.get_instance(post_id)
        if not post or post.discussion_id != discussion.id:
            return HTTPSeeOther(request.route_url(
                'home', discussion_slug=discussion.slug))
        context['post'] = post
    elif route_name == "purl_idea":
        idea_id = FrontendUrls.getRequestedIdeaId(request)
        if not idea_id:
            return HTTPSeeOther(request.route_url(
                'home', discussion_slug=discussion.slug))
        idea = Idea.get_instance(idea_id)
        if not idea or idea.discussion_id != discussion.id:
            return HTTPSeeOther(request.route_url(
                'home', discussion_slug=discussion.slug))
        context['idea'] = idea

    canAddExtract = user_has_permission(discussion.id, user_id, P_ADD_EXTRACT)
    context['canAddExtract'] = canAddExtract
    context['canDisplayTabs'] = True
    preferences = discussion.preferences
    session = discussion.db
    if user_id != Everyone:
        from assembl.models import UserPreferenceCollection
        user = User.get(user_id)
        preferences = UserPreferenceCollection(user_id, discussion)
        target_locale = get_locale_from_request(request, session, user)
    else:
        target_locale = get_locale_from_request(request, session)

    translation_service_data = {}
    try:
        service = discussion.translation_service()
        if service:
            translation_service_data = service.serviceData()
    except:
        pass
    context['translation_service_data_json'] = json.dumps(
        translation_service_data)
    locale_labels = json.dumps(
        DummyGoogleTranslationService.target_locale_labels_cls(target_locale))
    context['translation_locale_names_json'] = locale_labels

    context['preferences_json'] = json.dumps(dict(preferences))
    role_names = [x for (x) in session.query(Role.name).all()]
    context['role_names'] = json.dumps(role_names)

    response = render_to_response('../../templates/index.jinja2', context,
                                  request=request)
    # Prevent caching the home, especially for proper login/logout
    response.cache_control.max_age = 0
    response.cache_control.prevent_auto = True
    return response


@view_config(route_name='styleguide', request_method='GET', http_cache=60,
             renderer='assembl:templates/styleguide/index.jinja2')
def styleguide_view(request):
    context = get_default_context(request)
    context['styleguide_views'] = get_styleguide_components()
    return context


@view_config(route_name='test', request_method='GET', http_cache=60,
             renderer='assembl:templates/tests/index.jinja2')
def frontend_test_view(request):
    context = get_default_context(request)
    discussion = context["discussion"]
    target_locale = Locale.get_or_create('en', discussion.db)
    locale_labels = json.dumps(
        DummyGoogleTranslationService.target_locale_labels_cls(target_locale))
    context['translation_locale_names_json'] = locale_labels
    context['translation_service_data_json'] = '{}'
    context['preferences_json'] = json.dumps(dict(discussion.preferences))
    return context


@view_config(context=HTTPNotFound, renderer='assembl:templates/includes/404.jinja2')
def not_found(context, request):
    request.response.status = 404
    return {}
