/**
 * Copyright 2012-2021 Canonical Ltd. This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 *
 * Tests for lp.services.messages.edit.
 *
 * @module lp.services.messages.edit
 * @submodule test
 */

YUI.add('lp.services.messages.edit.test', function(Y) {

    var namespace = Y.namespace('lp.services.messages.edit.test');

    var suite = new Y.Test.Suite("lp.services.messages.edit Tests");
    var module = Y.lp.services.messages.edit;

    function assertDisplayStyles(items, visibility) {
        for(var i=items ; i<items.length ; i++) {
             Y.Assert.areSame(visibility, items[i].getStyle("display"));
        }
    }

    function assertDisplayStyle(item, visibility) {
        Y.Assert.areSame(visibility, item.getStyle("display"));
    }

    var TestMessageEdit = {
        name: "TestMessageEdit",

        setUp: function() {
            this.containers = [
                Y.one("#first-message"), Y.one("#second-message")];
            this.last_edit = [
                this.containers[0].one(".editable-message-last-edit-date"),
                this.containers[1].one(".editable-message-last-edit-date")
            ];
            this.revision_history_link = [
                this.containers[0].one(".editable-message-last-edit-link"),
                this.containers[1].one(".editable-message-last-edit-link")
            ];
            this.revision_history_lists = [
                this.containers[0].one(".message-revision-list"),
                this.containers[1].one(".message-revision-list")
            ];
            this.msg_bodies = [
                this.containers[0].one(".editable-message-body"),
                this.containers[1].one(".editable-message-body")
            ];
            this.msg_texts = [
                this.containers[0].one(".editable-message-text"),
                this.containers[1].one(".editable-message-text")
            ];
            this.msg_forms = [
                this.containers[0].one(".editable-message-form"),
                this.containers[1].one(".editable-message-form")
            ];
            this.edit_icons = [
                this.containers[0].one(".editable-message-edit-btn"),
                this.containers[1].one(".editable-message-edit-btn")
            ];
            this.cancel_btns = [
                this.containers[0].one(".editable-message-cancel-btn"),
                this.containers[1].one(".editable-message-cancel-btn")
            ];
            this.textareas = [
                this.msg_forms[0].one("textarea"),
                this.msg_forms[1].one("textarea")
            ];
            this.update_btns = [
                this.containers[0].one(".editable-message-update-btn"),
                this.containers[1].one(".editable-message-update-btn")
            ];
            this.delete_btns = [
                this.containers[0].one(".editable-message-delete-btn"),
                this.containers[1].one(".editable-message-delete-btn")
            ];

            for(var i=0 ; i<this.containers.length ; i++) {
                this.msg_texts[i].getDOMNode().innerHTML = (
                    "Message number " + i);
                this.msg_bodies[i].setStyle('display', '');
                this.msg_forms[i].setStyle('display', '');
                this.textareas[i].getDOMNode().value = '';
                this.revision_history_lists[i].getDOMNode().innerHTML = '';
            }
            this.last_edit[0].getDOMNode().innerHTML = ':';
            this.last_edit[1].getDOMNode().innerHTML = (
                '(last edit 5 minutes ago):');
        },

        test_instantiation_hides_forms: function() {
            // When editable messages are initialized, the forms should be
            // hidden.
            module.setup();

            assertDisplayStyles(this.msg_bodies, 'block');
            assertDisplayStyles(this.msg_forms, 'none');
        },

        test_click_edit_icon_shows_form: function() {
            // Makes sure the form is shown when we click one of the edit icons.
            module.setup();
            this.edit_icons[1].simulate('click');

            // Form 1 should be visible...
            assertDisplayStyle(this.msg_bodies[1], 'none');
            assertDisplayStyle(this.msg_forms[1], 'block');

            // ... but form 0 should have not be affected.
            assertDisplayStyle(this.msg_bodies[0], 'block');
            assertDisplayStyle(this.msg_forms[0], 'none');
        },

        test_cancel_button_hides_form: function() {
            // Makes sure the form is hidden again if the user, after clicking
            // edit icons, decides to cancel edition.
            module.setup();
            this.edit_icons[1].simulate('click');
            this.cancel_btns[1].simulate('click');

            assertDisplayStyle(this.msg_bodies[0], 'block');
            assertDisplayStyle(this.msg_forms[0], 'none');
            assertDisplayStyle(this.msg_bodies[1], 'block');
            assertDisplayStyle(this.msg_forms[1], 'none');
        },

        test_success_save_comment_edition: function() {
            module.setup();
            module.lp_client.io_provider = new Y.lp.testing.mockio.MockIo();

            // Edit the comment index #1
            this.edit_icons[1].simulate('click');
            var new_message = 'edited\nmessage <foo>';
            var uri_encoded_new_message = encodeURI(new_message);
            this.textareas[1].getDOMNode().value = new_message;
            this.update_btns[1].simulate('click');

            // Checks that only the current form interactions are blocked.
            Y.Assert.isTrue(this.textareas[1].getDOMNode().disabled);
            Y.Assert.isTrue(this.update_btns[1].getDOMNode().disabled);
            Y.Assert.isFalse(this.textareas[0].getDOMNode().disabled);
            Y.Assert.isFalse(this.update_btns[0].getDOMNode().disabled);

            module.lp_client.io_provider.success({
                responseText:'null',
                responseHeaders: {'Content-Type': 'application/json'}
            });
            Y.Assert.areSame(
                '<p>edited<br>message &lt;foo&gt;</p>',
                this.msg_texts[1].getDOMNode().innerHTML);

            // All forms should be released.
            Y.Assert.isFalse(this.textareas[1].getDOMNode().disabled);
            Y.Assert.isFalse(this.update_btns[1].getDOMNode().disabled);
            Y.Assert.isFalse(this.textareas[0].getDOMNode().disabled);
            Y.Assert.isFalse(this.update_btns[0].getDOMNode().disabled);

            // Check forms and msg bodies visibility are back to normal.
            assertDisplayStyle(this.msg_bodies[0], 'block');
            assertDisplayStyle(this.msg_forms[0], 'none');
            assertDisplayStyle(this.msg_bodies[1], 'block');
            assertDisplayStyle(this.msg_forms[1], 'none');

            // Check that the request was made correctly.
            var last_request = module.lp_client.io_provider.last_request;
            Y.Assert.areSame("/api/devel/message/2", last_request.url);
            Y.Assert.areSame("POST", last_request.config.method);
            Y.Assert.areSame(
                "ws.op=editContent&new_content=" + uri_encoded_new_message,
                last_request.config.data);

            // Check that the "last edit" header changed.
            Y.Assert.areSame(":", this.last_edit[0].getDOMNode().innerHTML);
            Y.Assert.areSame(
                ' <a href="#" class="editable-message-last-edit-link">' +
                '(last edit a moment ago):</a>',
                this.last_edit[1].getDOMNode().innerHTML);
        },

        test_shows_revision_history: function() {
            module.setup();
            module.lp_client.io_provider = new Y.lp.testing.mockio.MockIo();
            var revisions_container = this.containers[1].one(
                '.message-revision-container');
            // Revisions container should not be shown before it's clicked.
            Y.Assert.areSame('none', revisions_container.getStyle('display'));

            // Simulates the click and the request.
            this.revision_history_link[1].simulate('click');

            var response = {
                "total_size": 1, "start": 0,
                "entries": [{
                    "revision": 1,
                    "content": "content 1",
                    "date_created_display": "2021-05-10 19:41:36 UTC"
                }, {
                    "revision": 2,
                    "content": "content 2",
                    "date_created_display": "2021-05-11 22:17:11 UTC"
                }]
            };
            module.lp_client.io_provider.success({
                responseText: Y.JSON.stringify(response),
                responseHeaders: {'Content-Type': 'application/json'}
            });

            // Make sure it didn't fill the first container.
            Y.Assert.areSame(
                "", this.revision_history_lists[0].getDOMNode().innerHTML);

            // Check that revision list pop-up is shown
            Y.Assert.areSame('block', revisions_container.getStyle('display'));

            // Check the items in the pop-up revisions list.
            var revisions = this.revision_history_lists[1].all(
                ".message-revision-item");
            Y.Assert.areSame(2, revisions.size());
            revisions.each(function(rev, i) {
                // Entries are shown in reverse order.
                var entry = response.entries[response.entries.length - i -1];
                var title = rev.one('.message-revision-title');
                var body = rev.one('.message-revision-body');
                var expected_title = Y.Lang.sub(
                    'Remove Revision #{revision},'+
                    ' created at {date_created_display}',
                    entry);
                Y.Assert.areEqual(
                    expected_title, title.getDOMNode().innerText);

                Y.Assert.areSame(
                    module.htmlify_msg(entry.content),
                    body.getDOMNode().innerHTML);
            });

            revisions = this.revision_history_lists[1].all(
                ".message-revision-item");
            Y.Assert.areSame(2, revisions.size());

            // Delete one revision from the revision list of
            // the second message
            revisions.each(function(rev, j) {
                var entry = response.entries[response.entries.length - j -1];
                var title = rev.one('.message-revision-title');
                var body = rev.one('.message-revision-body');

                if (j===0){
                    // mock user's response to modal widonw that is
                    // asking the confirmation to delete revision
                    module.confirm = function(){
                        return 'True';
                    };
                    // user clicks on the Remove icon for the revision
                    rev.one('.message-revision-del-btn').simulate('click');
                    module.lp_client.io_provider.success({
                        responseText: 'null',
                        responseHeaders: {'Content-Type': 'application/json'}
                    });

                    // the title displayed for the revision stays the same
                    var expected_title = Y.Lang.sub(
                        'Revision #{revision},' +
                        ' created at {date_created_display}',
                        entry);
                    Y.Assert.areSame(
                        expected_title,
                        title.getDOMNode().innerText);

                    // the content has changed to reflect deletion
                    var expected_body = Y.Lang.sub(
                        'Content deleted by the user.',
                        entry);
                    Y.Assert.areEqual(
                        expected_body, body.getDOMNode().innerText);
                }
            });
            // The number of revisions in the list stays the same
            // and we asserted above the content displayed has changed
            // to reflect deletion for revision 2
            var after_delete_revis = this.revision_history_lists[1].all(
                ".message-revision-item");
            Y.Assert.areSame(2, after_delete_revis.size());

            // Lets make sure that a click on the "close" button hides the
            // revisions list.
            revisions_container.one(
                '.message-revision-close').simulate('click');
            Y.Assert.areSame('none', revisions_container.getStyle('display'));
        },

        test_delete_comment: function() {
            module.setup();
            module.lp_client.io_provider = new Y.lp.testing.mockio.MockIo();
            module.confirm = function(){
                return 'True';
            };
            // Delete the comment
            this.delete_btns[1].simulate('click');
            module.lp_client.io_provider.success({
                responseText:'null',
                responseHeaders: {'Content-Type': 'application/json'}
            });

            // Check message was deleted
            Y.Assert.areSame(
                "",
                this.msg_texts[1].getDOMNode().innerHTML);

            // All forms should be released.
            Y.Assert.isFalse(this.textareas[1].getDOMNode().disabled);
            Y.Assert.isFalse(this.update_btns[1].getDOMNode().disabled);
            Y.Assert.isFalse(this.textareas[0].getDOMNode().disabled);
            Y.Assert.isFalse(this.update_btns[0].getDOMNode().disabled);

            // Check forms and msg bodies visibility are back to normal.
            assertDisplayStyle(this.msg_bodies[0], 'block');
            assertDisplayStyle(this.msg_forms[0], 'none');
            assertDisplayStyle(this.msg_bodies[1], 'block');
            assertDisplayStyle(this.msg_forms[1], 'none');

            // Check that the request was made correctly.
            var last_request = module.lp_client.io_provider.last_request;
            Y.Assert.areSame("/api/devel/message/2", last_request.url);
            Y.Assert.areSame("POST", last_request.config.method);
            Y.Assert.areSame(
                "ws.op=deleteContent",
                last_request.config.data);

            // Check that the "last edit" header changed.
            Y.Assert.areSame(":", this.last_edit[0].getDOMNode().innerHTML);
            Y.Assert.areSame(
                ' <a href="#" class="editable-message-last-edit-link">' +
                '(message and all revisions deleted a moment ago):</a>',
                this.last_edit[1].getDOMNode().innerHTML);

            // Check the pop-up revisions list is empty after deleting
            var revisions = this.revision_history_lists[1].all(
                ".message-revision-item");
            Y.Assert.areSame(0, revisions.size());
        },

        test_wire_new_messages: function() {
            // Editable messages inserted into the page after load (e.g. a
            // freshly posted comment) get their event handlers wired on
            // demand with wireNewMessages().
            var make_message_html = function(id, baseurl) {
                return (
                    '<div class="editable-message" id="' + id + '" ' +
                    'data-baseurl="' + baseurl + '" data-i-can-edit="True">' +
                    '<span class="editable-message-last-edit-date">:</span>' +
                    '<div class="editable-message-body">' +
                    '<div class="editable-message-text">New message</div>' +
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
            };

            // "root" may contain the editable messages as descendants.
            var wrapper = Y.Node.create('<div id="wire-new-wrapper"></div>');
            wrapper.getDOMNode().innerHTML = make_message_html(
                'third-message', '/message/3');
            Y.one(document.body).appendChild(wrapper);

            module.wireNewMessages(wrapper);

            // The edit button of the new message shows its edit form.
            var new_message = wrapper.one('#third-message');
            var msg_form = new_message.one('.editable-message-form');
            var msg_body = new_message.one('.editable-message-body');
            assertDisplayStyle(msg_form, 'none');
            assertDisplayStyle(msg_body, 'block');
            new_message.one('.editable-message-edit-btn').simulate('click');
            assertDisplayStyle(msg_body, 'none');
            assertDisplayStyle(msg_form, 'block');

            // "root" may also be the editable message container itself.
            var lone_message = Y.Node.create(
                make_message_html('fourth-message', '/message/4'));
            Y.one(document.body).appendChild(lone_message);
            module.wireNewMessages(lone_message);
            lone_message.one('.editable-message-edit-btn').simulate('click');
            assertDisplayStyle(
                lone_message.one('.editable-message-body'), 'none');
            assertDisplayStyle(
                lone_message.one('.editable-message-form'), 'block');

            wrapper.remove(true);
            lone_message.remove(true);
        }
    };

    suite.add(new Y.Test.Case(TestMessageEdit));

    namespace.suite = suite;

}, "0.1", {"requires": [
               "lp.services.messages.edit", "node", "lp.testing.mockio",
               "node-event-simulate", "test", "lp.anim"]});
