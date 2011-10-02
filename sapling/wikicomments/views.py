from django.contrib import comments, messages
from django.contrib.auth.decorators import login_required
from django.contrib.comments import signals
from django.contrib.comments import views
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.urlresolvers import reverse, NoReverseMatch
from django.db import models
from django.http import HttpResponseRedirect
from django.shortcuts import render, get_object_or_404
from django.utils.translation import ungettext, ugettext_lazy as _
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

import urllib

from models import WikiComment

@csrf_protect
@login_required
@require_POST
def post_comment(request, next=None, using=None):
    """
    Re-implementation (ugh) of django.contrib.comments.views.post_comment,
    to remove support for anonymous commenting, and to use the Messages
    framework.
    """
    # Fill out some initial data fields from an authenticated user, if present
    data = request.POST.copy()
    data["name"] = request.user.get_full_name() or request.user.username
    data["email"] = request.user.email

    # Check to see if the POST data overrides the view's next argument.
    next = data.get("next", next)
    if next is None:
        try:
            next = reverse('frontpage')
        except NoReverseMatch:
            next = '/'

    # Look up the object we're trying to comment about
    ctype = data.get("content_type")
    object_pk = data.get("object_pk")
    if ctype is None or object_pk is None:
        return views.CommentPostBadRequest("Missing content_type or object_pk field.")
    try:
        model = models.get_model(*ctype.split(".", 1))
        target = model._default_manager.using(using).get(pk=object_pk)
    except TypeError:
        return views.CommentPostBadRequest(
            "Invalid content_type value: %r" % escape(ctype))
    except AttributeError:
        return views.CommentPostBadRequest(
            "The given content-type %r does not resolve to a valid model." % \
                escape(ctype))
    except ObjectDoesNotExist:
        return views.CommentPostBadRequest(
            "No object matching content-type %r and object PK %r exists." % \
                (escape(ctype), escape(object_pk)))
    except (ValueError, ValidationError), e:
        return views.CommentPostBadRequest(
            "Attempting go get content-type %r and object PK %r exists raised %s" % \
                (escape(ctype), escape(object_pk), e.__class__.__name__))

    # Construct the comment form
    form = comments.get_form()(target, data=data)

    # Check security information
    if form.security_errors():
        return views.CommentPostBadRequest(
            "The comment form failed security verification: %s" % \
                escape(str(form.security_errors())))

    # If there are errors or if we requested a preview show the comment
    if form.errors:
        message = "<p>Your comment could not be submitted!</p>"
        message += str(form.errors)
        messages.add_message(request, messages.ERROR, message)
        return HttpResponseRedirect(next)

    # Otherwise create the comment
    comment = form.get_comment_object()
    if not comment:
        message = _("Your edit could not be saved, as the original comment " +
                    "could not be found!")
        messages.add_message(request, messages.ERROR, message)
        return HttpResponseRedirect(next)

    comment.ip_address = request.META.get("REMOTE_ADDR", None)
    comment.user = request.user

    # Signal that the comment is about to be saved
    responses = signals.comment_will_be_posted.send(
        sender  = comment.__class__,
        comment = comment,
        request = request
    )

    for (receiver, response) in responses:
        if response == False:
            return views.CommentPostBadRequest(
                "comment_will_be_posted receiver %r killed the comment" % receiver.__name__)

    # Save the comment and signal that it was saved
    comment.save(comment="Comment posted: %s%s" % (comment.comment[:50], '...' if len(comment.comment) > 50 else ''))
    signals.comment_was_posted.send(
        sender  = comment.__class__,
        comment = comment,
        request = request
    )

    # Prepare a message
    if 'comment_pk' in data:
        messages.add_message(request, messages.INFO,
            _("You have successfully edited your comment of %s." %
              comment.submit_date))
    else:
        messages.add_message(request, messages.INFO,
            _("Thank you for your comment, %s!" % comment.name))

    # Change the URL so we see a non-cached page
    if comment._get_pk_val():
        payload = {'c': comment._get_pk_val()}
        if '#' in next:
            tmp = next.rsplit('#', 1)
            next = tmp[0]
            anchor = '#' + tmp[1]
        else:
            anchor = ''

        joiner = ('?' in next) and '&' or '?'
        next += joiner + urllib.urlencode(payload) + anchor

    return HttpResponseRedirect(next)

@csrf_protect
@login_required
@require_POST
def edit_comment(request, next=None, using=None):
    """
    Handles receipt of an edited comment.
    """
    # TODO: For this view, consider sticking the security data in the id=?

    if not request.is_ajax():
        return views.CommentPostBadRequest("Request is not AJAX.")

    # Fill out some initial data fields from an authenticated user, if present
    data = request.POST.copy()
    data["name"] = request.user.get_full_name()

    # Look up the object we're trying to comment about
    comment_pk = data.get("comment_pk")
    try:
        comment = WikiComment.objects.get(pk=comment_pk)
        target = comment.content_object
    except ObjectDoesNotExist:
        return views.CommentPostBadRequest(
            "No object matching comment_pk %r could be found." % escape(comment_pk))

    # Ensure the user has ability to edit the comment
    if request.user != comment.user:
        return views.CommentPostBadRequest("User did not post this comment!")
    elif not comment.is_public or comment.is_removed:
        return views.CommentPostBadRequest("Comment is no longer public.")

    # Update the comment instance.
    comment.comment = data.get("comment")

    # Signal that the comment is about to be saved
    responses = signals.comment_will_be_posted.send(
        sender  = comment.__class__,
        comment = comment,
        request = request
    )

    for (receiver, response) in responses:
        if response == False:
            return views.CommentPostBadRequest(
                "comment_will_be_posted receiver %r killed the comment" % receiver.__name__)

    # Save the comment and signal that it was saved
    comment.save(comment="Comment of %s edited: %s%s" % (comment.submit_date, comment.comment[:50], '...' if len(comment.comment) > 50 else ''))
    signals.comment_was_posted.send(
        sender  = comment.__class__,
        comment = comment,
        request = request
    )

    # Prepare a message
    messages.add_message(request, messages.INFO,
        _("You have successfully edited your comment of %s." %
          comment.submit_date))

    return render(request, 'comments/includes/comment_body.html', {'comment':comment})

@csrf_protect
@login_required
@require_POST
def delete_comment(request, next=None, using=None):
    """
    Handles deletion of comments.
    """
    if not request.is_ajax():
        return views.CommentPostBadRequest("Request is not AJAX.")

    # Fill out some initial data fields from an authenticated user, if present
    data = request.POST.copy()

    # Look up the object we're trying to comment about
    comment_pk = data.get("comment_pk")
    try:
        comment = WikiComment.objects.get(pk=comment_pk)
        target = comment.content_object
    except ObjectDoesNotExist:
        return views.CommentPostBadRequest(
            "No object matching comment_pk %r could be found." % escape(comment_pk))

    # Ensure the user has ability to edit the comment
    if request.user != comment.user:
        return views.CommentPostBadRequest("User did not post this comment!")
    elif not comment.is_public or comment.is_removed:
        return views.CommentPostBadRequest("Comment is no longer public.")

    # Nuke it
    comment.is_removed = True

    # Signal that the comment is about to be saved
    responses = signals.comment_will_be_posted.send(
        sender  = comment.__class__,
        comment = comment,
        request = request
    )

    for (receiver, response) in responses:
        if response == False:
            return views.CommentPostBadRequest(
                "comment_will_be_posted receiver %r killed the comment" % receiver.__name__)

    # Save the comment and signal that it was saved
    comment.save(comment="Comment of %s deleted" % (comment.submit_date))
    signals.comment_was_posted.send(
        sender  = comment.__class__,
        comment = comment,
        request = request
    )

    # Prepare a message
    messages.add_message(request, messages.INFO,
        _("Your comment of %s has been deleted." %
          comment.submit_date))

    return render(request, 'comments/includes/comment_body.html', {'comment':comment})

def fetch_comment(request):
    """
    Returns an HTML snippet of a comment.
    """
    comment_pk = request.GET.get('comment_pk', None)
    format = request.GET.get('format', 'pretty')

    comment = get_object_or_404(WikiComment, pk=comment_pk)

    if not comment.is_public or comment.is_removed:
        return views.CommentPostBadRequest("Comment is no longer public.")

    return render(request, 'comments/includes/comment_body.html', {'comment':comment, 'format':format})