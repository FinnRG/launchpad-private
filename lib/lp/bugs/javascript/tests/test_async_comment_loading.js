/* Copyright 2011-2012 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE). */

YUI.add('lp.bugs.async_comments.test', function (Y) {

    // Local aliases.
    const Assert = Y.Assert;
    const module = Y.lp.bugs.bugtask_index;

    const commentsMarkup =
        "<div>This is a comment</div>" +
        "<div>So is this</div>" +
        "<div>So is this</div>" +
        "<div>And this, too.</div>";


    const tests = Y.namespace('lp.bugs.async_comments.test');
    tests.suite = new Y.Test.Suite('Async Comment Tests');

    // YUI's selector engine stamps a generated id on elements that have
    // none when they are used as the root of a scoped query (e.g.
    // wireNewMessages() calling root.all() on the wrapper inserted by
    // load_more_comments). Strip those ids so assertions can compare
    // against the literal markup that was loaded.
    function innerHtmlWithoutWrapperIds(node) {
        return node.get('innerHTML').replace(
            / id="yui_[0-9_]+"/g, '');
    }

    tests.suite.add(new Y.Test.Case({
        name: 'Basic async comment loading tests',

        setUp: function() {
            // Monkeypatch LP to avoid network traffic and to make
            // some things work as expected.
            Y.lp.client.Launchpad.prototype.named_post =
              function(url, func, config) {
                config.on.success();
              };
            LP = {
                'cache': {
                    'bug': {
                        self_link: "http://bugs.example.com/bugs/1234"
                    },
                    'context': {
                        self_link: "http://bugs.example.com/bugs/1234"
                    }
                }
            };
            // Some tests monkey-patch load_more_comments, so we save it
            // here to restore it after each test.
            this.old_load_more_comments = module.load_more_comments;

            // Add some HTML to the page for us to use.
            this.comments_container = Y.Node.create(
                '<div id="comments-container"></div>');
            this.add_comment_form_container = Y.Node.create(
                '<div id="add-comment-form-container" class="hidden"></div>');
            Y.one('body').appendChild(this.comments_container);
            Y.one('body').appendChild(this.add_comment_form_container);
        },

        tearDown: function() {
            this.comments_container.remove();
            this.add_comment_form_container.remove();
            // Restore load_more_comments in case it's been monkey-patched.
            module.load_more_comments = this.old_load_more_comments;
        },

        /**
         * load_more_comments() calls the passed batch_commments_url of the
         * current bug task and loads more comments from it.
         */
        test_load_more_comments_loads_more_comments: function() {
            const mockio = new Y.lp.testing.mockio.MockIo();
            module.load_more_comments(
                '', this.comments_container, mockio);
            mockio.success({
                responseText: commentsMarkup,
                responseHeaders: {'Content-Type': 'application/xhtml'}
            });
            Assert.areEqual(
                '<div>' + commentsMarkup + '</div>',
                innerHtmlWithoutWrapperIds(this.comments_container));
        },

        /**
         * load_more_comments() wires the edit/delete buttons of the newly
         * loaded comments, so that they work without a page reload.
         */
        test_load_more_comments_wires_edit_buttons: function() {
            const editableMessageMarkup =
                '<div class="boardComment editable-message" ' +
                'data-baseurl="/message/1" data-i-can-edit="True">' +
                '<span class="editable-message-last-edit-date">:</span>' +
                '<div class="editable-message-body">' +
                '<div class="editable-message-text">Some text</div>' +
                '</div>' +
                '<img class="sprite edit action-icon ' +
                'editable-message-edit-btn" />' +
                '<div class="editable-message-form">' +
                '<textarea></textarea>' +
                '<input type="button" value="Update" ' +
                'class="editable-message-update-btn" />' +
                '<input type="button" value="Cancel" ' +
                'class="editable-message-cancel-btn" />' +
                '</div>' +
                '</div>';
            const mockio = new Y.lp.testing.mockio.MockIo();
            module.load_more_comments(
                '', this.comments_container, mockio);
            mockio.success({
                responseText: editableMessageMarkup,
                responseHeaders: {'Content-Type': 'application/xhtml'}
            });
            // Clicking the edit button of a loaded comment shows its form.
            const container = this.comments_container.one('.editable-message');
            Assert.isObject(container);
            const msgForm = container.one('.editable-message-form');
            const msgBody = container.one('.editable-message-body');
            Assert.areEqual('none', msgForm.getStyle('display'));
            Assert.areEqual('block', msgBody.getStyle('display'));
            container.one('.editable-message-edit-btn').simulate('click');
            Assert.areEqual('none', msgBody.getStyle('display'));
            Assert.areEqual('block', msgForm.getStyle('display'));
        },

        /**
         * load_more_comments() sets the display style on the comment
         * container to "block" so that its contents don't end up
         * overflowing other parts of the page.
         */
        test_load_more_comments_sets_container_display_style: function() {
            const mockio = new Y.lp.testing.mockio.MockIo();
            module.load_more_comments(
                '', this.comments_container, mockio);
            mockio.success({
                responseText: commentsMarkup,
                responseHeaders: {'Content-Type': 'application/xhtml'}
            });
            Assert.areEqual(
                'block', this.comments_container.getStyle('display'));
        },

        /**
         * load_more_comments() will show the "add comment" form once all
         * the comments have loaded.
         */
        test_load_more_comments_shows_add_comment_form: function() {
            const addCommentFormContainer = Y.one(
                '#add-comment-form-container');
            Assert.isTrue(addCommentFormContainer.hasClass('hidden'));
            const mockio = new Y.lp.testing.mockio.MockIo();
            module.load_more_comments(
                '', this.comments_container, mockio);
            mockio.success({
                responseText: commentsMarkup,
                responseHeaders: {'Content-Type': 'application/xhtml'}
            });
            Assert.isFalse(addCommentFormContainer.hasClass('hidden'));
        },

        /**
         * load_more_comments() will call itself recursively until there are
         * no more comments to load.
         */
        test_load_more_comments_is_recursive: function() {
            const nextBatchUrlDiv =
                '<div id="next-batch-url">https://launchpad.test/</div>';
            const moreCommentsToLoadMarkup =
                '<div>Here, have a comment. There are more where this came' +
                'from</div>';
            const mockio = new Y.lp.testing.mockio.MockIo();
            module.load_more_comments(
                '', this.comments_container, mockio);
            mockio.success({
                responseText: nextBatchUrlDiv + moreCommentsToLoadMarkup,
                responseHeaders: {'Content-Type': 'application/xhtml'}
            });
            mockio.success({
                responseText: commentsMarkup,
                responseHeaders: {'Content-Type': 'application/xhtml'}
            });
            const expectedMarkup =
                '<div>' + moreCommentsToLoadMarkup + '</div>' +
                '<div>' + commentsMarkup + '</div>';
            Assert.areEqual(
                expectedMarkup,
                innerHtmlWithoutWrapperIds(this.comments_container));
        },

        /**
         * setup_show_more_comments_link() will set the onClick handler for
         * the link passed to it, and will also add a js-action class to the
         * link.
         */
        test_setup_show_more_comments_link_jsifies_link: function() {
            // We monkey-patch load_mode_comments so that we can use it to
            // test whether the link has been JS-ified properly.
            let loadMoreCommentsCalled = false;
            module.load_more_comments = () => {
                loadMoreCommentsCalled = true;
            };
            const link = Y.Node.create('<a href="#">A link</a>');
            module.setup_show_more_comments_link(link, '#', Y.Node.create());
            Assert.isTrue(link.hasClass('js-action'));
            link.simulate('click');
            Assert.isTrue(loadMoreCommentsCalled);
        },

        /**
         * setup_load_comments() will call load_more_comments if it's told
         * to.
         */
        test_setup_load_comments_calls_load_more_comments: function() {
            // Monkey-patch load_more_comments for the purposes of this
            // test.
            let loadMoreCommentsCalled = false;
            module.load_more_comments = () => {
                loadMoreCommentsCalled = true;
            };
            // A parameterless call to setup_load_comments won't call
            // load_more_comments.
            module.setup_load_comments();
            Assert.isFalse(loadMoreCommentsCalled);
            // Passing true for the load_more_comments parameter will cause
            // load_more_comments to be called.
            module.setup_load_comments(true);
            Assert.isTrue(loadMoreCommentsCalled);
        }

    }));
}, '0.1', {
    requires: ['test', 'lp.testing.helpers', 'test-console',
        'lp.bugs.bugtask_index', 'lp.client', 'event', 'node',
        'lp.testing.mockio', 'test', 'widget-stack', 'node-event-simulate',
        'lp.services.messages.edit']

});
