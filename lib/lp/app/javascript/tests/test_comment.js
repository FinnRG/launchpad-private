/* Copyright 2012-2018 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE). */

YUI.add('lp.app.comment.test', function (Y) {

    var tests = Y.namespace('lp.app.comment.test');
    tests.suite = new Y.Test.Suite('lp.app.comment Tests');

    tests.suite.add(new Y.Test.Case({
        name: 'lp.app.comment_tests',

        test_library_exists: function () {
            Y.Assert.isObject(Y.lp.app.comment,
                "Could not locate the lp.app.comment module");
        },

        test_init: function () {
            var comment = new Y.lp.app.comment.Comment();
            Y.Assert.isObject(comment.comment_input);
            Y.Assert.isObject(comment.submit_button);
            Y.Assert.isTrue(comment.progress_message.hasClass(
                'update-in-progress-message'));
        },

        test_validation: function () {
            var comment = new Y.lp.app.comment.Comment();
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
            var mock_client = new Y.lp.testing.helpers.LPClient();
            mock_client.named_post.args = [];
            mock_client.get.args = [];
            var comment = new Y.lp.app.comment.Comment({
                animate: false,
                lp_client: mock_client
            });
            return comment;
        },

        test_add_comment_calls: function () {
            // Test which functions Comment.add_comment calls.
            var comment = this._get_mocked_comment();
            var progress_ui_called = false;
            var post_comment_called = false;
            comment.activateProgressUI = function () {
                progress_ui_called = true;
            };
            comment.post_comment = function () {
                post_comment_called = true;
            };
            comment.validate = function () { return true; };
            var mock_event = { halt: function () {} };

            comment.add_comment(mock_event);
            Y.Assert.isTrue(
                progress_ui_called, 'activateProgressUI not called');
            Y.Assert.isTrue(
                post_comment_called, 'post_comment not called');
        },

        test_get_comment_html: function () {
            // Test that get_comment_HTML has the correct args and calls the
            // correct callbacks.
            var comment = this._get_mocked_comment();
            var message_entry = {
                get: function (ignore) {
                    // `get` is only called with `self_link` in this function.
                    // If that changes, this will need to be mocked better.
                    return "https://example.com/comments/3";
                }
            };
            var callback_called = false;
            var callback = function () {
                callback_called = true;
            };
            comment.get_comment_HTML(message_entry, callback);
            Y.Assert.isTrue(callback_called);
            var client = comment.get('lp_client');
            var url = client.received[0][1][0].split("?")[0];
            Y.Assert.areEqual(url, 'https://example.com/comments/3');
        },

        test_post_comment: function () {
            // Test Comment.post_comment
            var comment = this._get_mocked_comment();
            var no_op_called = false;
            var no_op = function () {
                no_op_called = true;
            };
            comment.post_comment(no_op);
            var client = comment.get('lp_client');
            Y.Assert.areEqual(
                client.received[0][1][0],
                '/bug/1/',
                'post_comment called with bad bug url.');
            Y.Assert.areEqual(
                client.received[0][1][1],
                'newMessage',
                'post_comment called wrong named post.');
            Y.Assert.isTrue(no_op_called);
        },

        test_insert_comment_html: function () {
            // Test Comment.insert_comment_HTML
            var comment = this._get_mocked_comment();
            var reset_contents_called = false;
            comment.reset_contents = function () {
                reset_contents_called = true;
            };
            comment.insert_comment_HTML('<span id="fake"></span>');
            Y.Assert.isTrue(reset_contents_called);
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
            var msg_form = container.one('.editable-message-form');
            var msg_body = container.one('.editable-message-body');
            Y.Assert.areSame('none', msg_form.getStyle('display'));
            Y.Assert.areSame('block', msg_body.getStyle('display'));
            container.one('.editable-message-edit-btn').simulate('click');
            Y.Assert.areSame('none', msg_body.getStyle('display'));
            Y.Assert.areSame('block', msg_form.getStyle('display'));
        },

        test_insert_comment_html_wires_edit_buttons: function () {
            // A comment inserted by Comment.insert_comment_HTML has its
            // edit/delete buttons wired, without needing a page reload.
            var comment = this._get_mocked_comment();
            comment.reset_contents = function () {};
            comment.insert_comment_HTML(this._editable_message_html());
            var container = Y.one('#add-comment-form')
                .get('parentNode').one('.editable-message');
            Y.Assert.isObject(container);
            this._assert_edit_button_wired(container);
            container.remove();
        },
        test_insert_code_review_comment_html: function () {
            // Test CodeReviewComment.insert_comment_HTML
            window.LP = {
                cache: {
                    context: { self_link: '/mp/1/' }
                }
            };
            Y.one('body').appendChild(
                Y.Node.create('<div id="conversation"/>'));
            var mock_client = new Y.lp.testing.helpers.LPClient();
            mock_client.named_post.args = [];
            mock_client.get.args = [];
            var comment = new Y.lp.app.comment.CodeReviewComment({
                animate: false,
                lp_client: mock_client
            });
            var reset_contents_called = false;
            comment.reset_contents = function () {
                reset_contents_called = true;
            };
            var new_comment_node = null;
            comment.on('CodeReviewComment.appended', function (comment_node) {
                new_comment_node = comment_node;
            });
            var green_flash = Y.lp.anim.green_flash;
            var green_flashed = false;
            Y.lp.anim.green_flash = function () {
                return {
                    run: function () {
                        green_flashed = true;
                    }
                };
            };
            comment.insert_comment_HTML('<span id="fake"></span>');
            Y.Assert.isTrue(reset_contents_called);
            Y.Assert.isObject(new_comment_node);
            Y.Assert.isObject(Y.one('#fake'));
            Y.Assert.areEqual(new_comment_node.get('id'), 'fake');
            Y.Assert.isTrue(green_flashed);
            Y.lp.anim.green_flash = green_flash;
            Y.one('#conversation').remove();
        },

        test_insert_code_review_comment_html_wires_edit_buttons: function () {
            // A comment inserted by CodeReviewComment.insert_comment_HTML
            // has its edit/delete buttons wired, without a page reload.
            window.LP = {
                cache: {
                    context: { self_link: '/mp/1/' }
                }
            };
            Y.one('body').appendChild(
                Y.Node.create('<div id="conversation"/>'));
            var mock_client = new Y.lp.testing.helpers.LPClient();
            var comment = new Y.lp.app.comment.CodeReviewComment({
                animate: false,
                lp_client: mock_client
            });
            comment.reset_contents = function () {};
            var green_flash = Y.lp.anim.green_flash;
            Y.lp.anim.green_flash = function () {
                return { run: function () {} };
            };
            comment.insert_comment_HTML(this._editable_message_html());
            var container = Y.one('#conversation').one('.editable-message');
            Y.Assert.isObject(container);
            this._assert_edit_button_wired(container);
            Y.lp.anim.green_flash = green_flash;
            Y.one('#conversation').remove();
        }
    }));

}, '0.1', {'requires': ['test', 'lp.testing.helpers', 'test-console',
                        'lp.app.comment', 'lp.services.messages.edit',
                        'node-event-simulate']});
