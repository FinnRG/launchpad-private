-- Copyright 2026 Canonical Ltd.  This software is licensed under the
-- GNU Affero General Public License version 3 (see the file LICENSE).

SET client_min_messages=ERROR;

CREATE TABLE OAuthRefreshToken (
    id serial PRIMARY KEY,
    consumer integer NOT NULL REFERENCES OAuthConsumer,
    person integer NOT NULL REFERENCES Person,
    authenticator integer NOT NULL REFERENCES RegisteredAuthenticator,
    permission integer NOT NULL,
    date_created timestamp without time zone
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL,
    date_expires timestamp without time zone NOT NULL,
    date_last_used timestamp without time zone,
    date_revoked timestamp without time zone,
    revoked_by integer REFERENCES Person,
    key text NOT NULL UNIQUE,
    secret text NOT NULL,
    product integer REFERENCES Product,
    project integer REFERENCES Project,
    distribution integer REFERENCES Distribution,
    sourcepackagename integer REFERENCES SourcePackageName,
    CONSTRAINT oauthrefreshtoken__just_one_context CHECK (
        null_count(ARRAY[product, project, distribution]) >= 2),
    CONSTRAINT oauthrefreshtoken__sourcepackagename_needs_distro CHECK (
        sourcepackagename IS NULL OR distribution IS NOT NULL)
);

CREATE INDEX oauthrefreshtoken__consumer__idx
    ON OAuthRefreshToken (consumer);
CREATE INDEX oauthrefreshtoken__person__idx
    ON OAuthRefreshToken (person);
CREATE INDEX oauthrefreshtoken__authenticator__idx
    ON OAuthRefreshToken (authenticator);
CREATE INDEX oauthrefreshtoken__revoked_by__idx
    ON OAuthRefreshToken (revoked_by);
CREATE INDEX oauthrefreshtoken__product__idx
    ON OAuthRefreshToken (product);
CREATE INDEX oauthrefreshtoken__project__idx
    ON OAuthRefreshToken (project);
CREATE INDEX oauthrefreshtoken__distribution__idx
    ON OAuthRefreshToken (distribution);
CREATE INDEX oauthrefreshtoken__sourcepackagename__idx
    ON OAuthRefreshToken (sourcepackagename);

CREATE TABLE OAuthAuthenticationChallenge (
    id serial PRIMARY KEY,
    refresh_token integer NOT NULL
        REFERENCES OAuthRefreshToken ON DELETE CASCADE,
    challenge bytea NOT NULL,
    date_created timestamp without time zone
        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL,
    date_expires timestamp without time zone NOT NULL,
    consumed boolean DEFAULT FALSE NOT NULL,
    CONSTRAINT oauthauthenticationchallenge__challenge_length CHECK (
        octet_length(challenge) = 32)
);

CREATE INDEX oauthauthenticationchallenge__refresh_token__idx
    ON OAuthAuthenticationChallenge (refresh_token);
CREATE INDEX oauthauthenticationchallenge__date_expires__idx
    ON OAuthAuthenticationChallenge (date_expires);

ALTER TABLE OAuthAccessToken ADD COLUMN refresh_token integer;
ALTER TABLE OAuthAccessToken ADD CONSTRAINT oauthaccesstoken_refresh_token_fkey
    FOREIGN KEY (refresh_token) REFERENCES OAuthRefreshToken NOT VALID;

COMMENT ON TABLE OAuthRefreshToken IS 'A long-lived OAuth refresh token bound to a registered WebAuthn authenticator.';
COMMENT ON COLUMN OAuthRefreshToken.consumer IS 'The consumer that may exchange this grant.';
COMMENT ON COLUMN OAuthRefreshToken.person IS 'The person on whose behalf the consumer acts.';
COMMENT ON COLUMN OAuthRefreshToken.authenticator IS 'The registered authenticator required to mint new tokens.';
COMMENT ON COLUMN OAuthRefreshToken.permission IS 'The maximum access level granted to minted access tokens.';
COMMENT ON COLUMN OAuthRefreshToken.date_created IS 'When this refresh token was created.';
COMMENT ON COLUMN OAuthRefreshToken.date_expires IS 'When this refresh token stops being accepted.';
COMMENT ON COLUMN OAuthRefreshToken.date_last_used IS 'When this refresh token last minted an access token.';
COMMENT ON COLUMN OAuthRefreshToken.date_revoked IS 'When this refresh token was revoked, if it has been.';
COMMENT ON COLUMN OAuthRefreshToken.revoked_by IS 'The person who revoked this refresh token, if any.';
COMMENT ON COLUMN OAuthRefreshToken.key IS 'The public identifier for this refresh token.';
COMMENT ON COLUMN OAuthRefreshToken.secret IS 'The hash digest of this refresh token''s secret.';
COMMENT ON COLUMN OAuthRefreshToken.product IS 'The product associated with this token.';
COMMENT ON COLUMN OAuthRefreshToken.project IS 'The project associated with this token.';
COMMENT ON COLUMN OAuthRefreshToken.distribution IS 'The distribution associated with this token.';
COMMENT ON COLUMN OAuthRefreshToken.sourcepackagename IS 'The source package name associated with this token.';

COMMENT ON TABLE OAuthAuthenticationChallenge IS 'A short-lived, single-use WebAuthn challenge for an OAuth minting exchange.';
COMMENT ON COLUMN OAuthAuthenticationChallenge.refresh_token IS 'The hardware-bound refresh token for which this challenge was issued.';
COMMENT ON COLUMN OAuthAuthenticationChallenge.challenge IS 'The raw WebAuthn challenge bytes.';
COMMENT ON COLUMN OAuthAuthenticationChallenge.date_created IS 'When this challenge was created.';
COMMENT ON COLUMN OAuthAuthenticationChallenge.date_expires IS 'When this challenge stops being accepted.';
COMMENT ON COLUMN OAuthAuthenticationChallenge.consumed IS 'Whether an exchange attempt has consumed this challenge.';

COMMENT ON COLUMN OAuthAccessToken.refresh_token IS 'The hardware-bound refresh token that minted this access token.';


INSERT INTO LaunchpadDatabaseRevision VALUES (2211, 62, 0);
