/* Copyright 2012-2018 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE). */

YUI.add('lp.app.comment.test', function (Y) {

    const tests = Y.namespace('lp.app.comment.test');
    tests.suite = new Y.Test.Suite('lp.app.comment Tests');

    tests.suite.add(new Y.Test.Case({
        name: 'lp.app.comment_tests',

        test_library_exists: function () {
            Y.Assert.isObject(Y.lp.app.comment,
                "Could not locate the lp.app.comment module");
        },

        test_init: function () {
            const comment = new Y.lp.app.comment.Comment();
            Y.Assert.isObject(comment.comment_input);
            Y.Assert.isObject(comment.submit_button);
            Y.Assert.isTrue(comment.progress_message.hasClass(
                'update-in-progress-message'));
        },

        test_validation: function () {
            const comment = new Y.lp.app.comment.Comment();
            comment.comment_input.set('value', 'foo ');
            Y.Assert.isTrue(comment.validate());

            comment.comment_input.set('value', '    ');
            Y.Assert.isFalse(comment.validate());
        },

        _get_mocked_comment: function () {
            window.LP = {
                cache: {
                    bug: { self_link: '/bug/1/' }
                }
            };
            const mockClient = new Y.lp.testing.helpers.LPClient();
            mockClient.named_post.args = [];
            mockClient.get.args = [];
            const comment = new Y.lp.app.comment.Comment({
                animate: false,
                lp_client: mockClient
            });
            return comment;
        },

        test_add_comment_calls: function () {
            // Test which functions Comment.addComment calls.
            const comment = this._get_mocked_comment();
            let progressUiCalled = false;
            let postCommentCalled = false;
            comment.activateProgressUI = () => {
                progressUiCalled = true;
            };
            comment.postComment = () => {
                postCommentCalled = true;
            };
            comment.validate = () => { return true; };
            const mockEvent = { halt: () => {} };

            comment.addComment(mockEvent);
            Y.Assert.isTrue(
                progressUiCalled, 'activateProgressUI not called');
            Y.Assert.isTrue(
                postCommentCalled, 'postComment not called');
        },

        test_get_comment_html: function () {
            // Test that getCommentHTML has the correct args and calls the
            // correct callbacks.
            const comment = this._get_mocked_comment();
            const messageEntry = {
                get: (ignore) => {
                    // `get` is only called with `self_link` in this function.
                    // If that changes, this will need to be mocked better.
                    return "https://example.com/comments/3";
                }
            };
            let callbackCalled = false;
            const callback = () => {
                callbackCalled = true;
            };
            comment.getCommentHTML(messageEntry, callback);
            Y.Assert.isTrue(callbackCalled);
            const client = comment.get('lp_client');
            const url = client.received[0][1][0].split("?")[0];
            Y.Assert.areEqual(url, 'https://example.com/comments/3');
        },

        test_post_comment: function () {
            // Test Comment.postComment
            const comment = this._get_mocked_comment();
            let noOpCalled = false;
            const noOp = () => {
                noOpCalled = true;
            };
            comment.postComment(noOp);
            const client = comment.get('lp_client');
            Y.Assert.areEqual(
                client.received[0][1][0],
                '/bug/1/',
                'postComment called with bad bug url.');
            Y.Assert.areEqual(
                client.received[0][1][1],
                'newMessage',
                'postComment called wrong named post.');
            Y.Assert.isTrue(noOpCalled);
        },

        test_insert_comment_html: function () {
            // Test Comment.insertCommentHTML
            const comment = this._get_mocked_comment();
            let resetContentsCalled = false;
            comment.resetContents = () => {
                resetContentsCalled = true;
            };
            comment.insertCommentHTML('<span id="fake"></span>');
            Y.Assert.isTrue(resetContentsCalled);
            Y.Assert.isObject(Y.one('#fake'));
        },

        _editable_message_html: function () {
            return (
                '<div class="boardComment editable-message" ' +
                'data-baseurl="/message/1" data-i-can-edit="True">' +
                '<span class="editable-message-last-edit-date">:</span>' +
                '<div class="editable-message-body">' +
                '<div class="editable-message-text">Some text</div>' +
                '</div>' +
                '<img class="sprite edit action-icon ' +
                'editable-message-edit-btn" />' +
                '<img class="sprite remove action-icon ' +
                'editable-message-delete-btn" />' +
                '<div class="editable-message-form">' +
                '<textarea></textarea>' +
                '<input type="button" value="Update" ' +
                'class="editable-message-update-btn" />' +
                '<input type="button" value="Cancel" ' +
                'class="editable-message-cancel-btn" />' +
                '</div>' +
                '</div>');
        },

        _assert_edit_button_wired: function (container) {
            // Clicking the new comment's edit button shows its edit form.
            const msgForm = container.one('.editable-message-form');
            const msgBody = container.one('.editable-message-body');
            Y.Assert.areSame('none', msgForm.getStyle('display'));
            Y.Assert.areSame('block', msgBody.getStyle('display'));
            container.one('.editable-message-edit-btn').simulate('click');
            Y.Assert.areSame('none', msgBody.getStyle('display'));
            Y.Assert.areSame('block', msgForm.getStyle('display'));
        },

        test_insert_comment_html_wires_edit_buttons: function () {
            // A comment inserted by Comment.insertCommentHTML has its
            // edit/delete buttons wired, without needing a page reload.
            const comment = this._get_mocked_comment();
            comment.resetContents = () => {};
            comment.insertCommentHTML(this._editable_message_html());
            const container = Y.one('#add-comment-form')
                .get('parentNode').one('.editable-message');
            Y.Assert.isObject(container);
            this._assert_edit_button_wired(container);
            container.remove();
        },

        test_insert_comment_html_fires_appended: function () {
            // Comment.insertCommentHTML fires "Comment.appended" with the
            // inserted comment node, matching CodeReviewComment's behaviour.
            const comment = this._get_mocked_comment();
            comment.resetContents = () => {};
            let newCommentNode = null;
            comment.on('Comment.appended', (commentNode) => {
                newCommentNode = commentNode;
            });
            comment.insertCommentHTML('<span id="fake"></span>');
            Y.Assert.isObject(newCommentNode);
            Y.Assert.areEqual('fake', newCommentNode.get('id'));
            newCommentNode.remove();
        },

        test_insert_code_review_comment_html: function () {
            // Test CodeReviewComment.insertCommentHTML
            window.LP = {
                cache: {
                    context: { self_link: '/mp/1/' }
                }
            };
            Y.one('body').appendChild(
                Y.Node.create('<div id="conversation"/>'));
            const mockClient = new Y.lp.testing.helpers.LPClient();
            mockClient.named_post.args = [];
            mockClient.get.args = [];
            const comment = new Y.lp.app.comment.CodeReviewComment({
                animate: false,
                lp_client: mockClient
            });
            let resetContentsCalled = false;
            comment.resetContents = () => {
                resetContentsCalled = true;
            };
            let newCommentNode = null;
            comment.on('CodeReviewComment.appended', (commentNode) => {
                newCommentNode = commentNode;
            });
            const greenFlash = Y.lp.anim.green_flash;
            let greenFlashed = false;
            Y.lp.anim.green_flash = () => {
                return {
                    run: () => {
                        greenFlashed = true;
                    }
                };
            };
            comment.insertCommentHTML('<span id="fake"></span>');
            Y.Assert.isTrue(resetContentsCalled);
            Y.Assert.isObject(newCommentNode);
            Y.Assert.isObject(Y.one('#fake'));
            Y.Assert.areEqual(newCommentNode.get('id'), 'fake');
            Y.Assert.isTrue(greenFlashed);
            Y.lp.anim.green_flash = greenFlash;
            Y.one('#conversation').remove();
        },

        test_insert_code_review_comment_html_wires_edit_buttons: function () {
            // A comment inserted by CodeReviewComment.insertCommentHTML
            // has its edit/delete buttons wired, without a page reload.
            window.LP = {
                cache: {
                    context: { self_link: '/mp/1/' }
                }
            };
            Y.one('body').appendChild(
                Y.Node.create('<div id="conversation"/>'));
            const mockClient = new Y.lp.testing.helpers.LPClient();
            const comment = new Y.lp.app.comment.CodeReviewComment({
                animate: false,
                lp_client: mockClient
            });
            comment.resetContents = () => {};
            const greenFlash = Y.lp.anim.green_flash;
            Y.lp.anim.green_flash = () => {
                return { run: () => {} };
            };
            comment.insertCommentHTML(this._editable_message_html());
            const container = Y.one('#conversation').one('.editable-message');
            Y.Assert.isObject(container);
            this._assert_edit_button_wired(container);
            Y.lp.anim.green_flash = greenFlash;
            Y.one('#conversation').remove();
        }
    }));

}, '0.1', {'requires': ['test', 'lp.testing.helpers', 'test-console',
                        'lp.app.comment', 'lp.services.messages.edit',
                        'node-event-simulate']});
