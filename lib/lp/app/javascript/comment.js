/* Copyright 2026 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 *
 * Widgets for posting and moderating comments.
 *
 * This module provides three widgets:
 *
 *   CommentList        hides and unhides existing comments through the
 *                      admin "mark as spam" links.
 *   Comment            the "Add comment" form on bug pages; it posts a new
 *                      message, inserts its rendered HTML, and fires
 *                      "Comment.appended".
 *   CodeReviewComment  a Comment subclass for merge-proposal reviews; it
 *                      adds votes, review types, inline comments and
 *                      "Reply" handling, and fires
 *                      "CodeReviewComment.appended".
 *
 * Comment and CodeReviewComment wire the edit/delete buttons of a freshly
 * inserted comment by listening for their "*.appended" event and calling
 * lp.services.messages.edit.wireNewMessages(); messages.edit.setup() only
 * handles the comments already present when the page loads.
 *
 * @module lp.app.comment
 * @requires base, oop, io, widget, node, lp.client, lp.client.plugins,
 *     lp.app.errors, lp.services.messages.edit
 */

/** @import {EventFacade, YuiNode} from "./yui" */

/**
 * A message entry returned by the newMessage / createComment web-service
 * calls; getCommentHTML() reads one of its fields (e.g. "self_link" or
 * "id") to fetch the rendered comment.
 * @typedef {Object} MessageEntry
 * @property {function(string): *} get - Read one field of the entry.
 * @see Bug.newMessage in lib/lp/bugs/model/bug.py and
 *     BranchMergeProposal.createComment in
 *     lib/lp/code/model/branchmergeproposal.py.
 */

YUI.add('lp.app.comment', function(Y) {

const namespace = Y.namespace('lp.app.comment');

namespace.CommentList = Y.Base.create('commentlist', Y.Widget, [], {

    /**
     * Update a comment appearance on hide commands.
     *
     * @method updateComment
     * @param {YuiNode} link The hide/unhide link that was clicked.
     * @param {YuiNode} comment The comment node to update.
     */
    updateComment: function (link, comment) {
        const text = link.get('text').trim();
        if (text === this.get('hide_text')) {
            comment.removeClass(this.get('hidden_class'));
            link.set('text', this.get('unhide_text'));
        } else {
            comment.addClass(this.get('hidden_class'));
            link.set('text', this.get('hide_text'));
        }
    },

    /**
     * Set the comment visibility.
     *
     * @method setVisibility
     */
    setVisibility: function (parameters, callbacks) {
        // comment_context must be setup on pages using this js, and is the
        // context for a comment (e.g. bug).
        const commentContext = LP.cache.comment_context;
        const lpClient = new Y.lp.client.Launchpad();
        const config = {
            on: {
                success: callbacks.success,
                failure: callbacks.failure
                },
            parameters: parameters
            };
        lpClient.named_post(
            commentContext.self_link, 'setCommentVisibility', config);
    },

    /**
     * toggle the hidden status of the comment.
     *
     * @method toggleHidden
     * @param {EventFacade} e The click event on the "mark as spam" link.
     */
    toggleHidden: function (e) {
        e.halt();
        const link = e.target;
        const comment = link.get('parentNode').get('parentNode');
        const visible = comment.hasClass('adminHiddenComment');
        const commentNumber = parseInt(
                link.get('id').replace('mark-spam-', ''), 10);
        const parameters = {
            visible: visible,
            comment_number: commentNumber
            };
        this.setVisibility(parameters, {
            // We use red flash on failure so admins know it didn't work.
            // There's no green flash on success, b/c the change in bg
            // color provides an immediate visual cue.
            success: () => {
                this.updateComment(link, comment);
                comment.toggleClass(this.get('hidden_class'));
            },
            failure: () => {
                Y.lp.anim.red_flash({node: comment});
            }
        });
    },

    /**
     * Bind the UI to a new comment.
     *
     * @method bindNewComment
     * @param {YuiNode} commentNode The comment container to bind.
     */
    bindNewComment: function(commentNode) {
        commentNode.delegate(
            'click', this.toggleHidden, 'a.mark-spam', this);
    },

    destructor: function() {
        const commentListContainer = this.get('comment_list_container');
        commentListContainer.detachAll();
    },

    bindUI: function () {
        const commentListContainer = this.get('comment_list_container');
        commentListContainer.delegate(
            'click', this.toggleHidden, 'a.mark-spam', this);
    }
}, {
    ATTRS: {
        hidden_class: { value: "adminHiddenComment" },
        hide_text: { value: "Hide" },
        unhide_text: { value: "Unhide" },
        comment_list_container: {
            valueFn: function () {
                return Y.one('#maincontentsub');
            }
        }
    }
});

namespace.Comment = Y.Base.create('comment', Y.Widget, [], {
    /**
     * Initialize the Comment
     *
     * @method initializer
     */
    initializer: function(cfg) {
        this.submit_button = this.getSubmit();
        this.comment_input = Y.one(
            '#add-comment-form [id="field.comment"]');
        this.error_handler = new Y.lp.client.ErrorHandler();
        this.error_handler.clearProgressUI = Y.bind(
            this.clearProgressUI, this);
        this.error_handler.showError = (errorMsg) => {
            Y.lp.app.errors.display_error(this.submit_button, errorMsg);
        };
        this.progress_message = Y.Node.create(
            '<span class="update-in-progress-message">Saving...</span>');
        // Wire the edit/delete buttons of comments added after page load;
        // messages.edit.setup() only handles the comments already present.
        this.on('Comment.appended', (comment) => {
            Y.lp.services.messages.edit.wireNewMessages(comment);
        });
    },

    /**
     * Return the Submit button.
     *
     * This is provided so that it can be overridden in subclasses.
     *
     * @method getSubmit
     */
    getSubmit: function(){
        return Y.one('#add-comment-form input[id="field.actions.save"]');
    },
    /**
     * Implementation of Widget.renderUI.
     *
     * This redisplays the submit button, in case it has been hidden by
     * the web page.
     *
     * @method renderUI
     */
    renderUI: function() {
        this.submit_button.addClass('js-action');
        this.submit_button.setStyle('display', 'inline');
    },
    /**
     * Ensure that the widget's values are suitable for submission.
     *
     * The contents of the comment field must contain at least one
     * non-whitespace character.
     *
     * @method validate
     */
    validate: function() {
        return Y.Lang.trim(this.comment_input.get('value')) !== '';
    },
    /**
     * Make the widget enabled or disabled.
     *
     * @method setDisabled
     * @param disabled A boolean, true if the widget is disabled.
     */
    setDisabled: function(disabled){
        this.comment_input.set('disabled', disabled);
    },
    /**
     * Add the widget's comment as a new comment, updating the display.
     *
     * @method addComment
     * @param {EventFacade} e The click event on the submit button.
     */
    addComment: function(e){
        e.halt();
        /* Don't try to add an empty comment. */
        if (!this.validate()) {
            return;
        }
        this.activateProgressUI('Saving...');
        this.postComment((messageEntry) => {
            this.getCommentHTML(
                messageEntry, (html) => this.insertCommentHTML(html));
            this._addCommentSuccess();
        });
    },
    /**
     * A callable hook for firing extra events.
     */
    _addCommentSuccess: function() {},
    /**
     * Post the comment to the Launchpad API
     *
     * @method postComment
     * @param callback A callable to call if the post is successful.
     */
    postComment: function(callback) {
        const config = {
            on: {
                success: callback,
                failure: this.error_handler.getFailureHandler()
            },
            parameters: {content: this.comment_input.get('value')}
        };
        this.get('lp_client').named_post(
            LP.cache.bug.self_link, 'newMessage', config);
    },
    /**
     * Retrieve the HTML of the specified message entry.
     *
     * @method getCommentHTML
     * @param {MessageEntry} messageEntry The comment to get the HTML for.
     * @param callback On success, call this with the HTML of the comment.
     */
    getCommentHTML: function(messageEntry, callback){
        const config = {
            on: {
                success: callback
            },
            accept: Y.lp.client.XHTML
        };
        // Randomize the URL to fake out bad XHR caching.
        const randomness = '?' + Math.random();
        const messageEntryUrl = messageEntry.get('self_link') + randomness;
        this.get('lp_client').get(messageEntryUrl, config);
    },
    /**
     * Insert the specified HTML into the page and fire the
     * "Comment.appended" event.
     *
     * @method insertCommentHTML
     * @param {string} messageHtml The HTML of the comment to insert.
     */
    insertCommentHTML: function(messageHtml) {
        const fieldset = Y.one('#add-comment-form');
        const comment = Y.Node.create(messageHtml);
        fieldset.get('parentNode').insertBefore(comment, fieldset);
        this.resetContents();
        this.fire('Comment.appended', comment);
        if (this.get('animate')) {
            Y.lp.anim.green_flash({node: comment}).run();
        }
    },
    /**
     * Reset the widget to a blank state.
     *
     * @method resetContents
     */
    resetContents: function() {
          this.clearProgressUI();
          this.comment_input.set('value', '');
          this.syncUI();
    },
    /**
     * Activate indications of an operation in progress.
     *
     * @param message A user message describing the operation in progress.
     */
    activateProgressUI: function(message){
        this.progress_message.set('innerHTML', message);
        this.setDisabled(true);
        this.submit_button.get('parentNode').replaceChild(
            this.progress_message, this.submit_button);
    },
    /**
     * Stop indicating that an operation is in progress.
     *
     * @method clearProgressUI
     */
    clearProgressUI: function(){
          this.progress_message.get('parentNode').replaceChild(
              this.submit_button, this.progress_message);
          this.setDisabled(false);
    },


    /**
     * Implementation of Widget.bindUI: Bind events to methods.
     *
     * Key and mouse presses (e.g. mouse paste) call syncUI, in case the submit
     * button needs to be updated.  Clicking on the submit button invokes
     * addComment.
     *
     * @method bindUI
     */
    bindUI: function(){
        this.comment_input.on('keyup', this.syncUI, this);
        this.comment_input.on('mouseup', this.syncUI, this);
        this.submit_button.on('click', this.addComment, this);
    },
    /**
     * Implementation of Widget.syncUI: Update appearance according to state.
     *
     * This just updates the submit button.
     *
     * @method syncUI
     */
    syncUI: function(){
        this.submit_button.set('disabled', !this.validate());
    }
}, {
    ATTRS: {
        lp_client: {
            valueFn: function() { return new Y.lp.client.Launchpad(); }
        },
        animate: { value: true }
    }
});

namespace.CodeReviewComment = Y.Base.create(
    'codereviewcomment', namespace.Comment, [],
{
    /**
     * Initialize the CodeReviewComment
     *
     * @method initializer
     */
    initializer: function() {
        this.vote_input = Y.one('[id="field.vote"]');
        this.review_type = Y.one('[id="field.review_type"]');
        this.in_reply_to = null;
        // Wire the edit/delete buttons of comments added after page load;
        // messages.edit.setup() only handles the comments already present.
        this.on('CodeReviewComment.appended', (comment) => {
            Y.lp.services.messages.edit.wireNewMessages(comment);
        });
    },
    /**
     * Return the Submit button.
     *
     * @method getSubmit
     */
    getSubmit: function(){
        return Y.one('[id="field.actions.add"]');
    },
    /**
     * Return the vote value selected, or null if none is selected.
     *
     * @method getVote
     */
    getVote: function() {
        const selectedIdx = this.vote_input.get('selectedIndex');
        const selected = this.vote_input.get('options').item(selectedIdx);
        if (selected.get('value') === ''){
            return null;
        }
        return selected.get('innerHTML');
    },
    /**
     * Whether or not the review comment has inline comments to be published
     *
     * @method hasInline
     */
    hasInline: function() {
        const inlineCb = Y.one('[id="field.publish_inline_comments"]');
        return inlineCb !== null && inlineCb.get('checked');
    },
    /**
     * Ensure that the widget's values are suitable for submission.
     *
     * This allows the vote to be submitted, even when no text is specified
     * for the comment.
     *
     * @method validate
     */
    validate: function(){
        if (this.getVote() !== null) {
            return true;
        }
        if (this.hasInline()) {
            return true;
        }
        return namespace.Comment.prototype.validate.apply(this);
    },
    /**
     * Make the widget enabled or disabled.
     *
     * @method setDisabled
     * @param disabled A boolean, true if the widget is disabled.
     */
    setDisabled: function(disabled){
        namespace.Comment.prototype.setDisabled.call(this, disabled);
        this.vote_input.set('disabled', disabled);
        this.review_type.set('disabled', disabled);
    },
    /**
     * Post the comment to the Launchpad API
     *
     * @method postComment
     * @param callback A callable to call if the post is successful.
     */
    postComment: function(callback) {
        const config = {
            on: {
                success: callback,
                failure: this.error_handler.getFailureHandler()
            },
            parameters: {
                content: this.comment_input.get('value'),
                subject: '',
                review_type: this.review_type.get('value'),
                vote: this.getVote()
            }
        };
        if (this.in_reply_to !== null) {
            config.parameters.parent = this.in_reply_to.get('self_link');
        }

        if (this.hasInline()) {
            const ic = Y.lp.code.branchmergeproposal.inlinecomments;
            config.parameters.inline_comments = ic.inlinecomments;
            config.parameters.previewdiff_id = ic.current_previewdiff_id;
        }

        this.get('lp_client').named_post(
            LP.cache.context.self_link, 'createComment', config);
    },
    /**
     * Retrieve the HTML of the specified message entry.
     *
     * @method getCommentHTML
     * @param {MessageEntry} commentEntry The comment to get the HTML for.
     * @param callback On success, call this with the HTML of the comment.
     */
    getCommentHTML: function(commentEntry, callback) {
        const fragmentUrl = 'comments/' + commentEntry.get('id') +
                           '/+fragment';
        Y.io(fragmentUrl, {
            on: {
                success: (id, response) => {
                    callback(response.responseText);
                },
                failure: this.error_handler.getFailureHandler()
            }
        });
    },
    /**
     * Event handler when a "Reply" link is clicked.
     *
     * @param {EventFacade} e The Event object representing the click.
     */
    replyClicked: function(e){
        e.halt();
        const replyLink = Y.lp.client.normalize_uri(e.target.get('href'));
        const rootUrl = replyLink.substr(0,
            replyLink.length - '+reply'.length);
        const objectUrl = '/api/devel' + rootUrl;
        this.activateProgressUI('Loading...');
        window.scrollTo(0, Y.one('#add-comment').getY());
        this.get('lp_client').get(objectUrl, {
            on: {
                success: (comment) => {
                    this.setInReplyTo(comment);
                    this.clearProgressUI();
                    this.syncUI();
                },
                failure: this.error_handler.getFailureHandler()
            }
        });
    },
    /**
     * Set the comment that the new comment will be in reply to.
     *
     * @param comment The comment to be in reply to.
     */
    setInReplyTo: function(comment) {
        this.in_reply_to = comment;
        this.comment_input.set('value', comment.get('as_quoted_email'));
    },
    /**
     * Reset the widget to a blank state.
     *
     * @method resetContents
     */
    resetContents: function() {
          this.review_type.set('value', '');
          this.vote_input.set('selectedIndex', 0);
          this.in_reply_to = null;
          namespace.Comment.prototype.resetContents.apply(this);
    },
    /**
     * Insert the specified HTML into the page and fire the
     * "CodeReviewComment.appended" event.
     *
     * @method insertCommentHTML
     * @param {string} messageHtml The HTML of the comment to insert.
     */
    insertCommentHTML: function(messageHtml){
        const conversation = Y.one('[id=conversation]');
        const comment = Y.Node.create(messageHtml);
        conversation.appendChild(comment);
        this.resetContents();
        this.fire('CodeReviewComment.appended', comment);
        Y.lp.anim.green_flash({node: comment}).run();
    },


    /**
     * Implementation of Widget.bindUI: Bind events to methods.
     *
     * In addition to Comment behaviour, mouseups and keyups on the vote and
     * review type cause a sync.
     *
     * @method bindUI
     */
    bindUI: function() {
        namespace.Comment.prototype.bindUI.apply(this);
        this.vote_input.on('keyup', this.syncUI, this);
        this.vote_input.on('change', this.syncUI, this);
        this.review_type.on('keyup', this.syncUI, this);
        this.review_type.on('mouseup', this.syncUI, this);
        Y.all('a.menu-link-reply').on('click', this.replyClicked, this);
        Y.on('CodeReviewComment.SET_DISABLED', (disabled) => {
            // this.setDisabled() unfortunately does too much for the
            // current callsite (inlinecomment).
            this.submit_button.set('disabled', disabled);
        });
    },
    /**
     * Implementation of Widget.syncUI: Update appearance according to state.
     *
     * This enables and disables the review type, in addition to Comment
     * behaviour.
     *
     * @method syncUI
     */
    syncUI: function() {
        namespace.Comment.prototype.syncUI.apply(this);
        const reviewTypeDisabled = (this.getVote() === null);
        this.review_type.set('disabled', reviewTypeDisabled);
    },
    /**
     * A callable hook for firing extra events.
     */
    _addCommentSuccess: function() {
        const VOTES_TABLE_PATH = '+votes';
        Y.io(VOTES_TABLE_PATH, {
            on: {
                success: (id, response) => {
                    const target = Y.one('#votes-target');
                    target.set('innerHTML', response.responseText);

                    const username = LP.links.me.substring(2);
                    const newReviewer = Y.one('#review-' + username);
                    if (Y.Lang.isValue(newReviewer)) {
                        const anim = Y.lp.anim.green_flash({
                            node: newReviewer});
                        anim.run();
                    }
                },
                failure: () => {}
            }
        });
    }
}, {});

}, "0.1" ,{"requires":["base", "oop", "io", "widget", "node", "lp.client",
                       "lp.client.plugins", "lp.app.errors",
                       "lp.services.messages.edit"]});
