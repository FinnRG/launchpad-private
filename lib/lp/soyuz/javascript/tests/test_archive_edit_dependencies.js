/* Copyright 2026 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE). 
*/


YUI.add('lp.soyuz.archive_edit_dependencies.test', function (Y) {
    var Assert = Y.Assert;  // For easy access to isTrue(), etc.
    var module = Y.lp.soyuz.archive_edit_dependencies;
    var tests = Y.namespace('lp.soyuz.archive_edit_dependencies.test');
    tests.suite = new Y.Test.Suite(
        'soyuz.archive_edit_dependencies Tests');

    // Browsers normalise computed font-weight differently ('bold' vs
    // '700'), so accept either spelling.
    var FONT_WEIGHTS = {
        bold: ['bold', '700'],
        normal: ['normal', '400']
    };

    function assert_font_weight(node, expected, message) {
        var actual = String(node.getStyle('fontWeight'));
        Assert.isTrue(
            Y.Array.indexOf(FONT_WEIGHTS[expected], actual) > -1,
            message + ' (got "' + actual + '")');
    }

    // Markup for one dependency row, as rendered server-side: the
    // priority is a plain text input and the row is only pre-checked
    // when the form is redisplayed after a validation error.
    function dependency_row(index, name, value, checked) {
        return [
            '<tr class="dependency-row">',
            '<td class="dependency-name">' + name + '</td>',
            '<td class="pin-priority-cell">',
            '<input type="text" name="field.priority_' + index + '" ',
            'value="' + value + '" />',
            '</td>',
            '<td>',
            '<label><input type="checkbox" ',
            'class="line-through-when-checked" ',
            'name="field.remove_' + index + '"',
            (checked ? ' checked="checked"' : ''), ' /> Remove</label>',
            '<span class="pending-removal-status"></span>',
            '</td>',
            '</tr>'
            ].join('');
    }

    // Reset both regions of the fixture that the module touches.
    function reset_fixture(options_html, rows_html) {
        Y.one('#highlight-options').set('innerHTML', options_html);
        Y.one('#ppa-dependencies').one('tbody').set('innerHTML', rows_html);
    }

    tests.suite.add(new Y.Test.Case({
        name: 'highlight-selected',

        setUp: function() {
            reset_fixture([
                '<label><input type="radio" class="highlight-selected" ',
                'name="deps" value="primary" checked="checked" /> ',
                'Primary</label>',
                '<label><input type="radio" class="highlight-selected" ',
                'name="deps" value="updates" /> Updates</label>'
                ].join(''), '');
            var inputs = Y.one('#highlight-options').all(
                'input.highlight-selected');
            this.primary = inputs.item(0);
            this.updates = inputs.item(1);
        },

        test_checked_label_is_bold: function() {
            // The label of the selected option is emboldened on setup.
            module.setup_archive_edit_dependencies();
            assert_font_weight(
                this.primary.get('parentNode'), 'bold',
                'The selected option should be bold after setup.');
        },

        test_unchecked_label_is_not_bold: function() {
            // Labels of unselected options are left at normal weight.
            module.setup_archive_edit_dependencies();
            assert_font_weight(
                this.updates.get('parentNode'), 'normal',
                'An unselected option should not be bold.');
        },

        test_clicking_moves_the_highlight: function() {
            // Selecting another option re-highlights the whole group, so
            // the previously selected label loses its bold weight.
            module.setup_archive_edit_dependencies();
            this.updates.set('checked', true);
            Y.Event.simulate(Y.Node.getDOMNode(this.updates), 'click');
            assert_font_weight(
                this.updates.get('parentNode'), 'bold',
                'The newly selected option should be bold.');
            assert_font_weight(
                this.primary.get('parentNode'), 'normal',
                'The previously selected option should no longer be bold.');
        }
    }));

    tests.suite.add(new Y.Test.Case({
        name: 'pin-priority-upgrade',

        setUp: function() {
            reset_fixture('', [
                '<tr class="dependency-row">',
                '<td class="dependency-name">ppa:launchpad/ppa</td>',
                '<td class="pin-priority-cell">',
                '<input type="hidden" name="field.priority_0-empty" ',
                'value="1" />',
                '<input type="text" name="field.priority_0" ',
                'value="500" />',
                '</td>',
                '</tr>'
                ].join(''));
            this.table = Y.one('#ppa-dependencies');
            this.cell = this.table.one('.pin-priority-cell');
        },

        test_priority_input_is_upgraded: function() {
            // The plain text input becomes a native numeric input wired
            // to the help text.
            module.setup_archive_edit_dependencies();
            var input = this.cell.one('input[name="field.priority_0"]');
            Assert.areEqual(
                'number', input.get('type'),
                'The priority input should become a number input.');
            Assert.isTrue(
                input.hasClass('pin-priority-input'),
                'The priority input should be marked as upgraded.');
            Assert.areEqual('1', input.getAttribute('step'));
            Assert.areEqual('numeric', input.getAttribute('inputmode'));
            Assert.areEqual(
                'pin-priority-help',
                input.getAttribute('aria-describedby'),
                'The input should point at the help text.');
        },

        test_priority_bounds_come_from_the_table: function() {
            // The bounds are taken from the server-rendered data
            // attributes so the client cannot drift from the model.
            module.setup_archive_edit_dependencies();
            var input = this.cell.one('input[name="field.priority_0"]');
            Assert.areEqual(
                this.table.getAttribute('data-priority-minimum'),
                input.getAttribute('min'),
                'min should come from data-priority-minimum.');
            Assert.areEqual(
                this.table.getAttribute('data-priority-maximum'),
                input.getAttribute('max'),
                'max should come from data-priority-maximum.');
        },

        test_priority_input_is_labelled_with_dependency_name: function() {
            // The column header alone is ambiguous out of context.
            module.setup_archive_edit_dependencies();
            var input = this.cell.one('input[name="field.priority_0"]');
            Assert.areEqual(
                'Pin priority for ppa:launchpad/ppa',
                input.getAttribute('aria-label'),
                'The input should be labelled with the dependency name.');
        },

        test_hidden_inputs_are_left_alone: function() {
            // Hidden inputs in the cell are not user-visible controls.
            module.setup_archive_edit_dependencies();
            var hidden = this.cell.one(
                'input[name="field.priority_0-empty"]');
            Assert.areEqual(
                'hidden', hidden.get('type'),
                'A hidden input should keep its type.');
            Assert.isFalse(
                hidden.hasClass('pin-priority-input'),
                'A hidden input should not be upgraded.');
        }
    }));

    tests.suite.add(new Y.Test.Case({
        name: 'pending-removal',

        setUp: function() {
            reset_fixture('', [
                dependency_row(0, 'ppa:foo/one', '500', false),
                dependency_row(1, 'ppa:foo/two', '100', true)
                ].join(''));
            var rows = Y.one('#ppa-dependencies').all('tr.dependency-row');
            this.row = rows.item(0);
            this.checked_row = rows.item(1);
            this.checkbox = this.row.one('input.line-through-when-checked');
            this.priority = this.row.one('.pin-priority-cell input');
        },

        // The handler is bound to both 'change' and 'click', so drive it
        // with an explicit checked state plus a 'change' event.
        toggle: function(checkbox, checked) {
            checkbox.set('checked', checked);
            Y.Event.simulate(Y.Node.getDOMNode(checkbox), 'change');
        },

        test_unchecked_row_is_not_pending: function() {
            // An unchecked row is left untouched by setup.
            module.setup_archive_edit_dependencies();
            Assert.isFalse(
                this.row.hasClass('pending-removal'),
                'An unchecked row should not be pending removal.');
            Assert.isFalse(
                this.priority.get('disabled'),
                'The priority input should stay enabled.');
            Assert.isNull(
                this.row.one('input.pin-priority-mirror'),
                'No mirror input should be created.');
            Assert.areEqual(
                '', this.row.one('.pending-removal-status').get('text'));
        },

        test_checking_marks_the_row_pending: function() {
            // Checking the box flags the row and announces the state.
            module.setup_archive_edit_dependencies();
            this.toggle(this.checkbox, true);
            Assert.isTrue(
                this.row.hasClass('pending-removal'),
                'The row should be marked as pending removal.');
            Assert.areEqual(
                module.PENDING_REMOVAL_TEXT,
                this.row.one('.pending-removal-status').get('text'),
                'The pending removal status should be announced.');
        },

        test_checking_disables_the_priority_input: function() {
            // The priority of a row being removed is not editable.
            module.setup_archive_edit_dependencies();
            this.toggle(this.checkbox, true);
            Assert.isTrue(
                this.priority.get('disabled'),
                'The priority input should be disabled.');
            Assert.areEqual(
                'true', this.priority.getAttribute('aria-disabled'),
                'The priority input should be marked aria-disabled.');
        },

        test_checkbox_stays_enabled: function() {
            // The removal must be undoable, so the checkbox stays live.
            module.setup_archive_edit_dependencies();
            this.toggle(this.checkbox, true);
            Assert.isFalse(
                this.checkbox.get('disabled'),
                'The removal checkbox should remain enabled.');
        },

        test_mirror_preserves_the_posted_value: function() {
            // Disabled controls are not submitted, so a hidden twin
            // carries the value the user typed.
            module.setup_archive_edit_dependencies();
            this.priority.set('value', '123');
            this.toggle(this.checkbox, true);
            var mirror = this.row.one('input.pin-priority-mirror');
            Assert.isNotNull(mirror, 'A mirror input should be created.');
            Assert.areEqual('hidden', mirror.get('type'));
            Assert.areEqual(
                this.priority.get('name'), mirror.get('name'),
                'The mirror should post under the same name.');
            Assert.areEqual(
                '123', mirror.get('value'),
                'The mirror should carry the current priority value.');
        },

        test_unchecking_restores_the_row: function() {
            // Undoing a removal reverts every change.
            module.setup_archive_edit_dependencies();
            this.toggle(this.checkbox, true);
            this.toggle(this.checkbox, false);
            Assert.isFalse(
                this.row.hasClass('pending-removal'),
                'The pending removal class should be removed.');
            Assert.isFalse(
                this.priority.get('disabled'),
                'The priority input should be re-enabled.');
            Assert.isNull(
                this.priority.getAttribute('aria-disabled') || null,
                'aria-disabled should be removed.');
            Assert.isNull(
                this.row.one('input.pin-priority-mirror'),
                'The mirror input should be removed.');
            Assert.areEqual(
                '', this.row.one('.pending-removal-status').get('text'),
                'The status should be cleared.');
        },

        test_repeated_toggling_creates_a_single_mirror: function() {
            // The handler runs for both 'click' and 'change', so it must
            // be idempotent and never post a duplicate value.
            module.setup_archive_edit_dependencies();
            this.toggle(this.checkbox, true);
            this.toggle(this.checkbox, true);
            this.toggle(this.checkbox, false);
            this.toggle(this.checkbox, true);
            Assert.areEqual(
                1, this.row.all('input.pin-priority-mirror').size(),
                'There should only ever be one mirror input.');
        },

        test_checked_row_is_pending_after_setup: function() {
            // When the form is redisplayed after a validation error the
            // pending state is restored without any interaction.
            module.setup_archive_edit_dependencies();
            var priority = this.checked_row.one('.pin-priority-cell input');
            var mirror = this.checked_row.one('input.pin-priority-mirror');
            Assert.isTrue(
                this.checked_row.hasClass('pending-removal'),
                'A pre-checked row should be pending on setup.');
            Assert.isTrue(
                priority.get('disabled'),
                'Its priority input should be disabled on setup.');
            Assert.isNotNull(
                mirror, 'Its mirror input should exist on setup.');
            Assert.areEqual(
                '100', mirror.get('value'),
                'The mirror should carry the rendered priority value.');
            Assert.areEqual(
                module.PENDING_REMOVAL_TEXT,
                this.checked_row.one('.pending-removal-status').get('text'));
        }
    }));

    tests.suite.add(new Y.Test.Case({
        name: 'help-tip',

        setUp: function() {
            reset_fixture('', [
                '<tr class="dependency-row">',
                '<td class="pin-priority-cell">',
                '<span class="help-tip">',
                '<a href="#" class="help-trigger">?</a>',
                '<span class="help-text">Higher priorities win.</span>',
                '</span>',
                '<input type="text" name="field.priority_0" ',
                'value="500" />',
                '</td>',
                '</tr>'
                ].join(''));
            this.tip = Y.one('#ppa-dependencies .help-tip');
            this.trigger = this.tip.one('.help-trigger');
        },

        test_clicking_the_trigger_toggles_the_tip: function() {
            // Hover and focus are handled by CSS; touch devices need a
            // click toggle.
            module.setup_archive_edit_dependencies();
            Y.Event.simulate(Y.Node.getDOMNode(this.trigger), 'click');
            Assert.isTrue(
                this.tip.hasClass('help-tip-open'),
                'Clicking the trigger should open the tip.');
            Y.Event.simulate(Y.Node.getDOMNode(this.trigger), 'click');
            Assert.isFalse(
                this.tip.hasClass('help-tip-open'),
                'Clicking the trigger again should close the tip.');
        },

        test_escape_dismisses_the_tip: function() {
            // WCAG 1.4.13 requires hover/focus content to be dismissable.
            module.setup_archive_edit_dependencies();
            Y.Event.simulate(Y.Node.getDOMNode(this.trigger), 'click');
            Y.Event.simulate(
                Y.Node.getDOMNode(this.tip), 'keydown', {keyCode: 27});
            Assert.isFalse(
                this.tip.hasClass('help-tip-open'),
                'Escape should dismiss the tip.');
        }
    }));

}, '0.1', {
    requires: ['test', 'test-console', 'node-event-simulate',
        'lp.soyuz.archive_edit_dependencies']
});
