/* Copyright 2026 Canonical Ltd. This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 */

/**
 * Unit tests for hardware authenticator registration and revocation.
 *
 * The tests retain the real Launchpad named_post implementation.
 * MockIo intercepts the transport, while navigator.credentials.create
 * is replaced with a controlled Promise.
 *
 * Do not replace named_post_async with a callback-style fake. That
 * misrepresents its Promise interface and can hide the callback mismatch
 * these tests must detect.
 *
 * This suite requires neither a Launchpad login nor a physical
 * authenticator. Its HTML file loads dependencies, and each test creates
 * its own page fixture and global LP cache.
 *
 * @module lp.registry.person_register_authenticator.test
 */

YUI.add('lp.registry.person_register_authenticator.test', function(Y) {

    /*
     * The implementation namespace includes ".javascript".
     * The test suite's name matches the companion HTML's suites list.
     */
    const module = Y.lp.registry.javascript.person_register_authenticator;
    const tests = Y.namespace(
        'lp.registry.person_register_authenticator.test');

    /*
     * Use the website API path expected by the frontend client.
     * The real client normalizes SELF_LINK to API_PATH.
     * MockIo prevents requests from reaching this host.
     */
    const SELF_LINK = 'https://launchpad.test/api/devel/~testuser';
    const API_PATH = '/api/devel/~testuser';
    const LIST_URL =
        '/~testuser/+register-authenticator-authenticators-list';

    /**
     * Override browser properties that may normally be getter-only.
     *
     * Their original descriptors are restored during teardown.
     */
    function replaceProperty(object, name, value) {
        Object.defineProperty(object, name, {
            value: value,
            writable: true,
            configurable: true
        });
    }

    function restoreProperty(object, name, descriptor) {
        if (descriptor) {
            Object.defineProperty(object, name, descriptor);
        } else {
            delete object[name];
        }
    }

    /**
     * Create a Promise whose settlement is controlled by the test.
     *
     * Leaving it pending models the browser waiting for the user to
     * activate an authenticator.
     */
    function deferred() {
        let resolve;
        let reject;
        const promise = new Promise(function(resolvePromise, rejectPromise) {
            resolve = resolvePromise;
            reject = rejectPromise;
        });

        return {
            promise: promise,
            resolve: resolve,
            reject: reject
        };
    }

    /**
     * Wait for the fake WebAuthn Promise and its production callbacks.
     *
     * YUI.Test does not automatically wait for returned native Promises.
     * Explicit wait()/resume() calls keep the test active.
     *
     * Schedule resume on the next task so chained Promise callbacks
     * finish first. The zero-delay task is scheduled after settlement;
     * it is not a guessed registration duration.
     *
     * The wait timeout is a failure deadline. Assertions execute inside
     * resume() so YUI.Test reports their failures.
     */
    function resumeAfterSettlement(testCase, promise, assertions) {
        function settled() {
            Y.later(0, null, function() {
                testCase.resume(assertions);
            });
        }

        promise.then(settled, settled);
        testCase.wait(2000);
    }

    /**
     * Return the outer wrapper from beginWebAuthnRegistration.
     *
     * Binary values use base64url strings. Production code must convert
     * them before invoking navigator.credentials.create().
     */
    function creationOptions() {
        return {
            publicKey: {
                challenge: 'AQID',
                user: {
                    id: 'BAUG',
                    name: 'test-user',
                    displayName: 'Test User'
                },
                rp: {
                    name: 'Launchpad',
                    id: 'launchpad.test'
                },
                pubKeyCredParams: [
                    {type: 'public-key', alg: -7}
                ],
                authenticatorSelection: {
                    userVerification: 'required'
                },
                excludeCredentials: [
                    {
                        type: 'public-key',
                        id: 'BwgJ',
                        transports: ['usb']
                    }
                ]
            }
        };
    }

    /**
     * Return a browser-style credential for serialization tests.
     *
     * This is a structural fake, not a valid cryptographic attestation.
     * Backend tests cover attestation verification.
     */
    function credential() {
        return {
            id: 'credential-id',
            rawId: new Uint8Array([1, 2, 3]).buffer,
            type: 'public-key',
            authenticatorAttachment: 'cross-platform',
            getClientExtensionResults: function() {
                return {example: true};
            },
            response: {
                clientDataJSON: new Uint8Array([4, 5, 6]).buffer,
                attestationObject: new Uint8Array([7, 8, 9]).buffer,
                getTransports: function() {
                    return ['usb', 'nfc'];
                }
            }
        };
    }

    /**
     * Read form-encoded parameters produced by the real API client.
     *
     * Split each pair at its first "=" so encoded JSON remains intact.
     * These operations do not use repeated parameter names.
     */
    function requestParameters(request) {
        const parameters = {};

        request.config.data.split('&').forEach(function(pair) {
            const separator = pair.indexOf('=');
            parameters[decodeURIComponent(pair.slice(0, separator))] =
                decodeURIComponent(pair.slice(separator + 1));
        });

        return parameters;
    }

    function assertMessage(text) {
        Y.Assert.isTrue(
            Y.one('#messages-container').get('text').indexOf(text) !== -1,
            'Expected a message containing: ' + text);
    }

    function assertBrowserMessage(text) {
        Y.Assert.areEqual(
            text, Y.one('#messages-container .message').get('text'));
    }

    function assertIdle() {
        Y.Assert.isFalse(Y.one('#register-button').get('disabled'));
        Y.Assert.areEqual(
            'none', Y.one('#registration-spinner').getStyle('display'));
    }

    function assertBusy() {
        Y.Assert.isTrue(Y.one('#register-button').get('disabled'));
        Y.Assert.areEqual(
            'inline', Y.one('#registration-spinner').getStyle('display'));
    }

    /**
     * Begin registration and deliver a successful API response.
     *
     * MockIo invokes the real client's response handlers, exercising its
     * JSON decoding and callback contract.
     */
    function beginSuccessfully(testCase) {
        module.beginRegistration();
        Y.Assert.areEqual(1, testCase.io.requests.length);
        testCase.io.last_request.successJSON(creationOptions());
    }

    tests.suite = new Y.Test.Suite(
        'Hardware authenticator registration tests');

    tests.suite.add(new Y.Test.Case({
        name: 'Hardware authenticator frontend',

        setUp: function() {
            const testCase = this;

            /*
             * Remove handlers and timers left by the previous fixture
             * before replacing DOM nodes or globals.
             */
            module.destroy();

            this.originalLP = window.LP;
            this.originalIO = Y.io;
            this.originalConfirm = window.confirm;

            /*
             * Isolate the app namespace. Some tests replace its
             * confirmationoverlay property to use native confirmation.
             *
             * Do not assume Y.lp.app exists, and do not mutate the
             * original namespace object.
             */
            this.originalApp = Y.lp.app;
            Y.lp.app = Y.merge(this.originalApp || {});

            this.credentialsDescriptor =
                Object.getOwnPropertyDescriptor(navigator, 'credentials');
            this.publicKeyDescriptor =
                Object.getOwnPropertyDescriptor(window, 'PublicKeyCredential');

            /*
             * This is the global page cache, not the YUI namespace Y.lp.
             * The module reads it lazily.
             */
            window.LP = {
                cache: {
                    context: {self_link: SELF_LINK},
                    authenticators_list_url: LIST_URL
                },
                links: {}
            };

            const fixture = Y.one('#test-fixture');
            if (!fixture) {
                throw new Error(
                    'The test HTML must contain <div id="test-fixture">.');
            }

            fixture.setHTML(
                '<div id="browser-not-supported" ' +
                    'style="display: none">Browser not supported.</div>' +
                '<div id="messages-container"></div>' +
                '<div id="authenticators-list"></div>' +
                '<form id="add-authenticator-form" ' +
                    'style="display: block">' +
                    '<input id="authenticator-name" type="text" />' +
                    '<button id="register-button" type="button">' +
                        'Register</button>' +
                '</form>' +
                '<span id="registration-spinner" style="display: none">' +
                    'Registering...</span>');

            Y.one('#authenticator-name').set('value', 'Primary YubiKey');

            /*
             * The existing Launchpad client uses Y as its IO provider.
             * Replacing Y.io intercepts both API calls and list refreshes.
             * The client's methods themselves remain unchanged.
             */
            this.io = new Y.lp.testing.mockio.MockIo();
            Y.io = function(uri, config) {
                return testCase.io.io(uri, config);
            };

            this.creation = deferred();
            this.createCalls = 0;
            this.createdOptions = null;

            replaceProperty(navigator, 'credentials', {
                create: function(options) {
                    testCase.createCalls += 1;
                    testCase.createdOptions = options;
                    return testCase.creation.promise;
                }
            });

            /*
             * Use manual conversion by default, independently of the
             * host browser's parser. Specific tests install a parser.
             */
            replaceProperty(window, 'PublicKeyCredential', function() {});

            window.confirm = function() {
                return true;
            };
        },

        tearDown: function() {
            module.destroy();

            const fixture = Y.one('#test-fixture');
            if (fixture) {
                fixture.setHTML('');
            }

            Y.io = this.originalIO;
            window.LP = this.originalLP;
            window.confirm = this.originalConfirm;

            /* Restore the namespace, including its original absence. */
            Y.lp.app = this.originalApp;

            restoreProperty(
                navigator, 'credentials', this.credentialsDescriptor);
            restoreProperty(
                window, 'PublicKeyCredential', this.publicKeyDescriptor);
        },

        /* Module registration and binary conversion. */

        test_module_exports: function() {
            Y.Assert.isObject(module);

            [
                'initialize',
                'destroy',
                'showMessage',
                'getAuthenticatorsListURL',
                'refreshAuthenticators',
                'beginRegistration',
                'revokeAuthenticator',
                'base64urlToArrayBuffer',
                'arrayBufferToBase64url',
                'convertCreationOptions',
                'convertCredentialResponse'
            ].forEach(function(name) {
                Y.Assert.isFunction(module[name], name);
            });
        },

        test_base64url_encoding: function() {
            Y.Assert.areEqual(
                'SGVsbG8',
                module.arrayBufferToBase64url(
                    new Uint8Array([72, 101, 108, 108, 111]).buffer));
            Y.Assert.areEqual(
                '-__v',
                module.arrayBufferToBase64url(
                    new Uint8Array([251, 255, 239]).buffer));
        },

        test_base64url_decoding: function() {
            const bytes = new Uint8Array(
                module.base64urlToArrayBuffer('SGVsbG8'));

            Y.ArrayAssert.itemsAreEqual(
                [72, 101, 108, 108, 111],
                Array.prototype.slice.call(bytes));
        },

        test_base64url_round_trip: function() {
            const original = new Uint8Array([1, 2, 3, 250, 251, 252]);
            const decoded = new Uint8Array(
                module.base64urlToArrayBuffer(
                    module.arrayBufferToBase64url(original.buffer)));

            Y.ArrayAssert.itemsAreEqual(
                Array.prototype.slice.call(original),
                Array.prototype.slice.call(decoded));
        },

        test_empty_base64url_values: function() {
            Y.Assert.areEqual(
                '',
                module.arrayBufferToBase64url(new Uint8Array([]).buffer));
            Y.Assert.areEqual(
                0, module.base64urlToArrayBuffer('').byteLength);
        },

        /* Creation options must preserve the non-binary server data. */

        test_creation_options_convert_binary_fields: function() {
            const converted = module.convertCreationOptions(
                creationOptions().publicKey);

            Y.Assert.isTrue(converted.challenge instanceof ArrayBuffer);
            Y.Assert.isTrue(converted.user.id instanceof ArrayBuffer);
            Y.Assert.isTrue(
                converted.excludeCredentials[0].id instanceof ArrayBuffer);
        },

        test_creation_options_preserve_other_fields: function() {
            const converted = module.convertCreationOptions(
                creationOptions().publicKey);

            Y.Assert.areEqual('test-user', converted.user.name);
            Y.Assert.areEqual('Test User', converted.user.displayName);
            Y.Assert.areEqual('launchpad.test', converted.rp.id);
            Y.Assert.areEqual('Launchpad', converted.rp.name);
            Y.Assert.areEqual(-7, converted.pubKeyCredParams[0].alg);
            Y.Assert.areEqual(
                'required',
                converted.authenticatorSelection.userVerification);
            Y.ArrayAssert.itemsAreEqual(
                ['usb'], converted.excludeCredentials[0].transports);
        },

        test_creation_options_are_not_mutated: function() {
            const options = creationOptions().publicKey;
            const original = JSON.stringify(options);

            module.convertCreationOptions(options);

            Y.Assert.areEqual(original, JSON.stringify(options));
        },

        test_optional_excluded_credentials: function() {
            const options = creationOptions().publicKey;
            delete options.excludeCredentials;

            Y.Assert.isUndefined(
                module.convertCreationOptions(options).excludeCredentials);

            options.excludeCredentials = [];
            Y.Assert.areEqual(
                0,
                module.convertCreationOptions(options)
                    .excludeCredentials.length);
        },

        /* Credential serialization and backend payload compatibility. */

        test_native_credential_serialization: function() {
            const expected = {id: 'credential-id', type: 'public-key'};
            const result = module.convertCredentialResponse({
                toJSON: function() {
                    return expected;
                }
            });

            Y.Assert.areSame(expected, result);
        },

        test_fallback_credential_serialization: function() {
            const result = module.convertCredentialResponse(credential());

            Y.Assert.areEqual('credential-id', result.id);
            Y.Assert.areEqual('public-key', result.type);
            Y.Assert.areEqual(
                'cross-platform', result.authenticatorAttachment);
            Y.Assert.areEqual('AQID', result.rawId);
            Y.Assert.areEqual('BAUG', result.response.clientDataJSON);
            Y.Assert.areEqual('BwgJ', result.response.attestationObject);

            /*
             * The backend reads transports from the nested response.
             * Extension results must survive JSON serialization.
             */
            Y.ArrayAssert.itemsAreEqual(
                ['usb', 'nfc'], result.response.transports);
            Y.Assert.isTrue(
                JSON.parse(JSON.stringify(result))
                    .clientExtensionResults.example);
        },

        test_optional_credential_response_methods: function() {
            const value = credential();
            delete value.getClientExtensionResults;
            delete value.response.getTransports;

            const result = module.convertCredentialResponse(value);

            Y.Assert.areEqual(0, result.response.transports.length);
            Y.Assert.areEqual(
                0, Object.keys(result.clientExtensionResults).length);
        },

        /* Notifications and initial page validation. */

        test_messages_stack: function() {
            module.showMessage('First failure', 'error');
            module.showMessage('Second failure', 'error');

            Y.Assert.areEqual(
                2, Y.one('#messages-container').all('.message').size());
        },

        test_message_class_and_escaping: function() {
            const message = module.showMessage('<b>Warning</b>', 'warning');

            Y.Assert.isTrue(message.hasClass('warning'));
            Y.Assert.isTrue(message.hasClass('message'));
            Y.Assert.areEqual('<b>Warning</b>', message.get('text'));
            Y.Assert.isNull(message.one('b'));
        },

        test_unsupported_browser: function() {
            replaceProperty(navigator, 'credentials', undefined);

            module.initialize();

            Y.Assert.areEqual(
                'block',
                Y.one('#browser-not-supported').getStyle('display'));
            Y.Assert.areEqual(
                'none',
                Y.one('#add-authenticator-form').getStyle('display'));
            Y.Assert.areEqual(0, this.io.requests.length);
        },

        test_initialize_without_cache: function() {
            window.LP = undefined;

            module.initialize();

            assertMessage('This page is missing data it needs.');
            Y.Assert.areEqual(0, this.io.requests.length);
        },

        test_initialize_without_self_link: function() {
            window.LP.cache.context = {};

            module.initialize();

            assertMessage('This page is missing data it needs.');
            Y.Assert.areEqual(0, this.io.requests.length);
        },

        test_empty_name: function() {
            Y.one('#authenticator-name').set('value', '');

            module.beginRegistration();

            assertMessage('Please enter a name for your authenticator.');
            assertIdle();
            Y.Assert.areEqual(0, this.io.requests.length);
        },

        test_whitespace_name: function() {
            Y.one('#authenticator-name').set('value', '   ');

            module.beginRegistration();

            assertMessage('Please enter a name for your authenticator.');
            assertIdle();
            Y.Assert.areEqual(0, this.io.requests.length);
        },

        /*
         * Registration API contract.
         *
         * These tests keep the real named_post implementation to detect
         * callback/Promise interface mismatches.
         */

        test_begin_request_uses_real_client: function() {
            module.beginRegistration();

            const request = this.io.last_request;
            Y.Assert.areEqual(API_PATH, request.url);
            Y.Assert.areEqual('POST', request.config.method);
            Y.Assert.areEqual(
                'beginWebAuthnRegistration',
                requestParameters(request)['ws.op']);
            assertBusy();
        },

        test_successful_begin_reaches_webauthn: function() {
            beginSuccessfully(this);

            Y.Assert.areEqual(1, this.createCalls);
            Y.Assert.isTrue(
                this.createdOptions.publicKey.challenge instanceof
                    ArrayBuffer);
            Y.Assert.isTrue(
                this.createdOptions.publicKey.user.id instanceof
                    ArrayBuffer);
            assertBusy();
        },

        test_begin_failure_restores_controls: function() {
            module.beginRegistration();
            this.io.failure({
                status: 500,
                responseText: 'Could not begin'
            });

            assertIdle();
            assertMessage('Failed to begin registration: Could not begin');
            Y.Assert.areEqual(0, this.createCalls);
        },

        test_begin_dispatch_exception_restores_controls: function() {
            Y.io = function() {
                throw new Error('Transport setup failed');
            };

            module.beginRegistration();

            assertIdle();
            assertMessage(
                'Failed to begin registration: Transport setup failed');
            Y.Assert.areEqual(0, this.io.requests.length);
        },

        test_begin_without_context_restores_controls: function() {
            window.LP.cache.context = undefined;

            module.beginRegistration();

            assertIdle();
            assertMessage('This page is missing data it needs.');
            Y.Assert.areEqual(0, this.io.requests.length);
        },

        test_begin_without_self_link_restores_controls: function() {
            window.LP.cache.context = {};

            module.beginRegistration();

            assertIdle();
            assertMessage('This page is missing data it needs.');
            Y.Assert.areEqual(0, this.io.requests.length);
        },

        test_invalid_begin_response_restores_controls: function() {
            module.beginRegistration();
            this.io.last_request.successJSON({});

            assertIdle();
            assertMessage(
                'The server returned invalid registration options.');
            Y.Assert.areEqual(0, this.createCalls);
        },

        test_malformed_options_restore_controls: function() {
            const options = {challenge: 'AQID'};
            let expectedError;
            try {
                module.convertCreationOptions(options);
            } catch (error) {
                expectedError = error;
            }
            Y.Assert.isObject(expectedError);

            module.beginRegistration();
            this.io.last_request.successJSON({publicKey: options});

            assertIdle();
            assertBrowserMessage(expectedError.message);
            Y.Assert.areEqual(0, this.createCalls);
            Y.Assert.areEqual(1, this.io.requests.length);
        },

        test_duplicate_begin_is_ignored_while_busy: function() {
            module.beginRegistration();
            module.beginRegistration();

            Y.Assert.areEqual(1, this.io.requests.length);
        },

        /* Native parsing and manual fallback. */

        test_native_options_parser: function() {
            const expected = module.convertCreationOptions(
                creationOptions().publicKey);
            let parserCalls = 0;
            let receivedChallenge;

            window.PublicKeyCredential.parseCreationOptionsFromJSON =
                function(options) {
                    parserCalls += 1;
                    receivedChallenge = options.challenge;
                    return expected;
                };

            beginSuccessfully(this);

            Y.Assert.areEqual(1, parserCalls);
            Y.Assert.areEqual('AQID', receivedChallenge);
            Y.Assert.areSame(expected, this.createdOptions.publicKey);
            Y.Assert.areEqual(1, this.createCalls);
        },

        test_native_parser_failure_uses_manual_conversion: function() {
            window.PublicKeyCredential.parseCreationOptionsFromJSON =
                function() {
                    throw new Error('Native parser unavailable');
                };

            beginSuccessfully(this);

            Y.Assert.areEqual(1, this.createCalls);
            Y.Assert.isTrue(
                this.createdOptions.publicKey.challenge instanceof
                    ArrayBuffer);
        },

        test_synchronous_credential_error_restores_controls: function() {
            navigator.credentials.create = function() {
                throw new Error('Credential creation failed');
            };

            beginSuccessfully(this);

            assertIdle();
            assertBrowserMessage('Credential creation failed');
            Y.Assert.areEqual(1, this.io.requests.length);
        },

        /*
         * Frontend registration flow with mocked server responses.
         *
         * Server responses pass through MockIo, and the browser Promise
         * is settled explicitly. Assertions resume after its callbacks.
         */

        test_complete_registration_and_refresh: function() {
            const testCase = this;
            Y.one('#authenticator-name').set(
                'value', '  Primary YubiKey  ');
            beginSuccessfully(this);

            this.creation.resolve(credential());
            resumeAfterSettlement(this, this.creation.promise, function() {
                Y.Assert.areEqual(2, testCase.io.requests.length);

                const request = testCase.io.requests[1];
                const parameters = requestParameters(request);
                Y.Assert.areEqual('POST', request.config.method);
                Y.Assert.areEqual(API_PATH, request.url);
                Y.Assert.areEqual(
                    'completeWebAuthnRegistration', parameters['ws.op']);
                Y.Assert.areEqual('Primary YubiKey', parameters.name);

                // The API decodes the outer JSON layer to a Text value.
                // This checks the wire format, not Python marshalling.
                const responseText = JSON.parse(parameters.response);
                Y.Assert.isString(responseText);
                const response = JSON.parse(responseText);
                Y.Assert.areEqual('credential-id', response.id);
                Y.Assert.areEqual('AQID', response.rawId);
                Y.ArrayAssert.itemsAreEqual(
                    ['usb', 'nfc'], response.response.transports);

                request.successJSON(123);

                assertIdle();
                Y.Assert.areEqual(
                    '', Y.one('#authenticator-name').get('value'));
                assertMessage(
                    'Hardware authenticator registered successfully!');

                Y.Assert.areEqual(3, testCase.io.requests.length);
                const refresh = testCase.io.requests[2];
                Y.Assert.areEqual(LIST_URL, refresh.url);
                refresh.respond({
                    responseText: '<p>Primary YubiKey</p>'
                });

                Y.Assert.areEqual(
                    'Primary YubiKey',
                    Y.one('#authenticators-list').get('text'));
            });
        },

        test_complete_failure_restores_controls: function() {
            const testCase = this;
            beginSuccessfully(this);

            this.creation.resolve(credential());
            resumeAfterSettlement(this, this.creation.promise, function() {
                Y.Assert.areEqual(2, testCase.io.requests.length);
                testCase.io.failure({
                    status: 400,
                    responseText: 'Credential rejected'
                });

                assertIdle();
                assertMessage(
                    'Failed to complete registration: Credential rejected');
                Y.Assert.areEqual(2, testCase.io.requests.length);
                Y.Assert.areEqual(
                    'Primary YubiKey',
                    Y.one('#authenticator-name').get('value'));
            });
        },

        test_complete_dispatch_exception_restores_controls: function() {
            beginSuccessfully(this);
            Y.io = function() {
                throw new Error('Transport setup failed');
            };

            this.creation.resolve(credential());
            resumeAfterSettlement(this, this.creation.promise, function() {
                assertIdle();
                assertMessage(
                    'Failed to complete registration: ' +
                    'Transport setup failed');
            });
        },

        test_serialization_error_restores_controls: function() {
            const testCase = this;
            beginSuccessfully(this);

            this.creation.resolve({
                toJSON: function() {
                    throw new Error('Cannot serialize credential');
                }
            });
            resumeAfterSettlement(this, this.creation.promise, function() {
                assertIdle();
                assertMessage(
                    'Failed to process credential: ' +
                    'Cannot serialize credential');
                Y.Assert.areEqual(1, testCase.io.requests.length);
            });
        },

        /* Browser failures must restore controls without completing. */

        test_cancelled_ceremony_restores_controls: function() {
            const testCase = this;
            beginSuccessfully(this);

            const error = new Error('Cancelled');
            error.name = 'NotAllowedError';
            this.creation.reject(error);

            resumeAfterSettlement(this, this.creation.promise, function() {
                assertIdle();
                assertBrowserMessage(error.message);
                Y.Assert.areEqual(1, testCase.io.requests.length);
            });
        },

        test_security_error_preserves_browser_explanation: function() {
            const testCase = this;
            beginSuccessfully(this);

            const error = new Error(
                'WebAuthn is not supported on sites with TLS ' +
                'certificate errors.');
            error.name = 'SecurityError';
            this.creation.reject(error);

            resumeAfterSettlement(this, this.creation.promise, function() {
                assertIdle();
                assertBrowserMessage(error.message);
                Y.Assert.areEqual(1, testCase.io.requests.length);
            });
        },

        test_not_allowed_error_preserves_browser_explanation: function() {
            const testCase = this;
            beginSuccessfully(this);

            const error = new Error(
                'WebAuthn is not supported on sites with TLS ' +
                'certificate errors.');
            error.name = 'NotAllowedError';
            this.creation.reject(error);

            resumeAfterSettlement(this, this.creation.promise, function() {
                assertIdle();
                assertBrowserMessage(error.message);
                Y.Assert.areEqual(1, testCase.io.requests.length);
            });
        },

        test_constraint_error_is_explained: function() {
            beginSuccessfully(this);

            const error = new Error('User verification unavailable');
            error.name = 'ConstraintError';
            this.creation.reject(error);

            resumeAfterSettlement(this, this.creation.promise, function() {
                assertIdle();
                assertBrowserMessage(error.message);
            });
        },

        test_already_registered_key_is_explained: function() {
            beginSuccessfully(this);

            const error = new Error('Credential excluded');
            error.name = 'InvalidStateError';
            this.creation.reject(error);

            resumeAfterSettlement(this, this.creation.promise, function() {
                assertIdle();
                assertBrowserMessage(error.message);
            });
        },

        /*
         * A lost challenge cannot be resumed. The UI must explain that
         * the user needs to start a new registration attempt.
         */

        test_lost_challenge_shows_retry_advice: function() {
            module.beginRegistration();
            this.io.failure({
                status: 400,
                responseText: 'No WebAuthn registration is in progress.'
            });

            assertIdle();
            assertMessage('Your registration attempt timed out.');
        },

        test_expired_challenge_shows_retry_advice: function() {
            const testCase = this;
            beginSuccessfully(this);

            this.creation.resolve(credential());
            resumeAfterSettlement(this, this.creation.promise, function() {
                testCase.io.failure({
                    status: 400,
                    responseText: 'This registration challenge has expired.'
                });

                assertIdle();
                assertMessage('Your registration attempt timed out.');
            });
        },

        /* Fragment URL selection and server-rendered list refresh. */

        test_configured_fragment_url: function() {
            Y.Assert.areEqual(
                LIST_URL,
                module.getAuthenticatorsListURL('/unrelated/path'));
        },

        test_fragment_url_falls_back_to_registration_page: function() {
            delete window.LP.cache.authenticators_list_url;

            Y.Assert.areEqual(
                LIST_URL,
                module.getAuthenticatorsListURL(
                    '/~testuser/+register-authenticator'));
            Y.Assert.areEqual(
                LIST_URL,
                module.getAuthenticatorsListURL(
                    '/~testuser/+register-authenticator/'));
            Y.Assert.isNull(
                module.getAuthenticatorsListURL('/unrelated/path'));
        },

        test_fragment_refresh_updates_markup_and_calls_callback: function() {
            let called = false;
            module.refreshAuthenticators(function() {
                called = true;
            });

            Y.Assert.areEqual(LIST_URL, this.io.last_request.url);
            this.io.last_request.respond({
                responseText: '<p>Updated authenticator list</p>'
            });

            Y.Assert.isTrue(called);
            Y.Assert.areEqual(
                'Updated authenticator list',
                Y.one('#authenticators-list').get('text'));
        },

        test_fragment_refresh_failure_preserves_markup: function() {
            Y.one('#authenticators-list').setHTML('<p>Existing list</p>');
            module.refreshAuthenticators();
            this.io.failure({status: 500});

            assertMessage('Failed to refresh the list of authenticators.');
            Y.Assert.areEqual(
                'Existing list',
                Y.one('#authenticators-list').get('text'));
        },

        test_fragment_dispatch_exception_is_reported: function() {
            Y.io = function() {
                throw new Error('Transport setup failed');
            };

            module.refreshAuthenticators();

            assertMessage('Failed to refresh the list of authenticators.');
        },

        /* Revocation uses the same real named-post client contract. */

        test_revoke_posts_authenticator_id: function() {
            module.revokeAuthenticator('123');

            const request = this.io.last_request;
            const parameters = requestParameters(request);
            Y.Assert.areEqual('POST', request.config.method);
            Y.Assert.areEqual(API_PATH, request.url);
            Y.Assert.areEqual(
                'revokeWebAuthnRegistration', parameters['ws.op']);

            /*
             * The transport is form-encoded, so the integer is
             * represented as a string on the wire.
             */
            Y.Assert.areEqual('123', parameters.authenticator_id);
        },

        test_successful_revoke_refreshes_list: function() {
            module.revokeAuthenticator(123);
            this.io.last_request.successJSON(null);

            assertMessage('Hardware authenticator revoked successfully.');
            Y.Assert.areEqual(2, this.io.requests.length);
            Y.Assert.areEqual(LIST_URL, this.io.last_request.url);
        },

        test_revoke_failure_is_reported: function() {
            module.revokeAuthenticator(123);
            this.io.failure({
                status: 400,
                responseText: 'Could not revoke'
            });

            assertMessage('Failed to revoke authenticator: Could not revoke');
            Y.Assert.areEqual(1, this.io.requests.length);
        },

        test_revoke_dispatch_exception_is_reported: function() {
            Y.io = function() {
                throw new Error('Transport setup failed');
            };

            module.revokeAuthenticator(123);

            assertMessage(
                'Failed to revoke authenticator: Transport setup failed');
        },

        /*
         * Exercise delegated clicks using the native-confirm fallback.
         * These tests do not depend on overlay widget interactions.
         */

        test_revoke_delegation_survives_replaced_list: function() {
            Y.lp.app.confirmationoverlay = null;
            module.initialize();

            // Simulate a fragment arriving after handlers were attached.
            Y.one('#authenticators-list').setHTML(
                '<button type="button" class="revoke-button" ' +
                    'data-auth-id="456" data-auth-name="Old Key">' +
                    'Revoke</button>');

            Y.one('#authenticators-list .revoke-button').simulate('click');

            Y.Assert.areEqual(1, this.io.requests.length);
            Y.Assert.areEqual(
                '456',
                requestParameters(this.io.last_request).authenticator_id);
        },

        test_cancelled_revoke_sends_no_request: function() {
            Y.lp.app.confirmationoverlay = null;
            window.confirm = function() {
                return false;
            };
            module.initialize();

            Y.one('#authenticators-list').setHTML(
                '<button type="button" class="revoke-button" ' +
                    'data-auth-id="456" data-auth-name="Old Key">' +
                    'Revoke</button>');

            Y.one('#authenticators-list .revoke-button').simulate('click');

            Y.Assert.areEqual(0, this.io.requests.length);
        },

        test_reinitialization_does_not_duplicate_revoke_handlers: function() {
            Y.lp.app.confirmationoverlay = null;
            module.initialize();
            module.initialize();

            Y.one('#authenticators-list').setHTML(
                '<button type="button" class="revoke-button" ' +
                    'data-auth-id="456" data-auth-name="Old Key">' +
                    'Revoke</button>');

            Y.one('#authenticators-list .revoke-button').simulate('click');

            Y.Assert.areEqual(1, this.io.requests.length);
        },

        /* Page event wiring and lifecycle cleanup. */

        test_register_button_starts_registration: function() {
            module.initialize();

            Y.one('#register-button').simulate('click');

            Y.Assert.areEqual(1, this.io.requests.length);
            Y.Assert.areEqual(
                'beginWebAuthnRegistration',
                requestParameters(this.io.last_request)['ws.op']);
        },

        test_form_submit_starts_registration: function() {
            module.initialize();

            Y.one('#add-authenticator-form').simulate('submit');

            Y.Assert.areEqual(1, this.io.requests.length);
            Y.Assert.areEqual(
                'beginWebAuthnRegistration',
                requestParameters(this.io.last_request)['ws.op']);
        },

        test_destroy_detaches_handlers: function() {
            module.initialize();
            module.destroy();

            Y.one('#register-button').simulate('click');

            Y.Assert.areEqual(0, this.io.requests.length);
            assertIdle();
        }
    }));

}, '0.1', {
    requires: [
        'lp.testing.runner',
        'test',
        'test-console',
        'node-event-simulate',
        'lp.client',
        'lp.testing.mockio',
        'lp.app.confirmationoverlay',
        'lp.registry.javascript.person_register_authenticator'
    ]
});
