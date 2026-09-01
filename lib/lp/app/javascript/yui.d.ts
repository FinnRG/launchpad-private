/* Copyright 2026 Canonical Ltd.  This software is licensed under the
 * GNU Affero General Public License version 3 (see the file LICENSE).
 */

/**
 * Editor-only types for YUI APIs used by Launchpad JavaScript modules.
 *
 * Import the types from JSDoc; adjust the relative path for the module.
 *
 * @example
 * // In lib/lp/app/javascript/example.js:
 * /** @import {LaunchpadClient, YuiNode} from "./yui" *\/
 */

/** A YUI DOM event passed to a node event handler. */
export interface EventFacade {
  /** The node that dispatched the event. */
  target: YuiNode;
  /** Prevent the event's default action and stop it propagating. */
  halt(): void;
  /** Prevent the browser's default response to the event. */
  preventDefault(): void;
}

/** A YUI wrapper returned by `Y.one()` or `Y.Node.create()`. */
export interface YuiNode {
  /** Add a CSS class to the node. */
  addClass(className: string): this;
  /** Return the value of an HTML attribute. */
  getAttribute(name: string): string;
  /** Return data stored on the node, using the caller-supplied type. */
  getData<T = unknown>(name: string): T;
  /** Report whether the node has a CSS class. */
  hasClass(className: string): boolean;
  /** Insert another node at the requested position. */
  insert(node: YuiNode, where: string): this;
  /** Subscribe to a DOM event on the node. */
  on(eventName: string, callback: (event: EventFacade) => void): unknown;
  /** Remove the node, optionally destroying its YUI wrapper. */
  remove(destroy?: boolean): void;
  /** Remove an HTML attribute from the node. */
  removeAttribute(name: string): this;
  /** Remove a CSS class from the node. */
  removeClass(className: string): this;
  /** Set a YUI node attribute. */
  set(name: string, value: unknown): this;
  /** Set an HTML attribute on the node. */
  setAttribute(name: string, value: string): this;
  /** Store arbitrary data on the node. */
  setData(name: string, value: unknown): this;
  /** Replace the node's contents with HTML. */
  setHTML(html: string): this;
}

/** A collection returned by `Y.all()`. */
export interface YuiNodeList {
  /** Run a callback for each node in the collection. */
  each(callback: (node: YuiNode) => void): this;
  /** Return the node at a zero-based index. */
  item(index: number): YuiNode;
  /** Return the number of nodes in the collection. */
  size(): number;
}

/** The HTTP response supplied to a Launchpad client failure callback. */
export interface LaunchpadResponse {
  /** Response body returned by the server. */
  responseText?: string;
  /** HTTP response status code. */
  status?: number;
  /** HTTP response reason phrase. */
  statusText?: string;
}

/** Callbacks and parameters for a named Launchpad API request. */
export interface LaunchpadRequestConfig<Result = unknown> {
  /** Named-operation arguments sent to the server. */
  parameters: Record<string, unknown>;
  /** Handlers invoked when the request completes. */
  on: {
    /** Handle a successful operation result. */
    success?: (result: Result) => void;
    /** Handle a failed operation response. */
    failure?: (id: unknown, response: LaunchpadResponse) => void;
  };
}

/** Parameters and options for a Promise-based named API request. */
export interface LaunchpadAsyncRequestConfig {
  /** Named-operation arguments sent to the server. */
  parameters: Record<string, unknown>;
  /** Additional options forwarded to the callback-based client. */
  [name: string]: unknown;
}

/** The named-operation methods exposed by `Y.lp.client.Launchpad`. */
export interface LaunchpadClient {
  /** Call a named GET operation on a Launchpad resource. */
  named_get<Result = unknown>(
    uri: string,
    operationName: string,
    config: LaunchpadRequestConfig<Result>,
  ): unknown;
  /**
   * Call a named GET operation and resolve its result.
   *
   * The Promise rejects with `LaunchpadResponse` when the request fails.
   */
  named_get_async<Result = unknown>(
    uri: string,
    operationName: string,
    config: LaunchpadAsyncRequestConfig,
  ): Promise<Result>;
  /** Call a named POST operation on a Launchpad resource. */
  named_post<Result = unknown>(
    uri: string,
    operationName: string,
    config: LaunchpadRequestConfig<Result>,
  ): unknown;
  /**
   * Call a named POST operation and resolve its result.
   *
   * The Promise rejects with `LaunchpadResponse` when the request fails.
   */
  named_post_async<Result = unknown>(
    uri: string,
    operationName: string,
    config: LaunchpadAsyncRequestConfig,
  ): Promise<Result>;
}

/** Constructs a Launchpad web-service client. */
export interface LaunchpadClientConstructor {
  /** Create a Launchpad web-service client. */
  new (config?: Record<string, unknown>): LaunchpadClient;
}

/** Launchpad client APIs installed on a YUI instance. */
export interface YuiClientNamespace {
  /** Launchpad web-service client constructor. */
  Launchpad: LaunchpadClientConstructor;
  /** Display serialized Launchpad notification messages. */
  display_notifications(payload: string): void;
}

/** Launchpad-specific namespaces installed on a YUI instance. */
export interface YuiNamespace {
  /** Launchpad web-service client APIs. */
  client: YuiClientNamespace;
}

/** Creates YUI node wrappers from HTML. */
export interface YuiNodeConstructor {
  /** Create a YUI node wrapper from an HTML string. */
  create(html: string): YuiNode;
}

/** The YUI instance passed to a `YUI.add()` module factory. */
export interface YuiInstance {
  /** JSON serialization helpers. */
  JSON: JSON;
  /** YUI node constructor and factory methods. */
  Node: YuiNodeConstructor;
  /** Launchpad-specific YUI namespaces. */
  lp: YuiNamespace;
  /** Select every node matching a CSS selector. */
  all(selector: string): YuiNodeList;
  /** Create or return a nested namespace on this YUI instance. */
  namespace<Namespace extends object = Record<string, unknown>>(
    name: string,
  ): Namespace;
  /** Select the first node matching a CSS selector. */
  one(selector: string): YuiNode | null;
}

/** Dependency metadata accepted by `YUI.add()`. */
export interface YuiModuleMetadata {
  /** YUI modules that must load before this module. */
  requires?: string[];
}

/** The global YUI module loader. */
export interface YuiStatic {
  /** Register a module with the global YUI loader. */
  add(
    name: string,
    factory: (Y: YuiInstance) => void,
    version: string,
    metadata?: YuiModuleMetadata,
  ): void;
}

/** Common fields available on `LP.cache.context`. */
export interface LaunchpadCacheContext {
  /** Time at which a merge was requested, or null when none is pending. */
  date_merge_requested?: string | null;
  /** Current queue state of the page's primary object. */
  queue_status?: string;
  /** Canonical API URI of the page's primary object. */
  self_link: string;
  /** Additional context values supplied by individual pages. */
  [name: string]: unknown;
}

/** Common values injected into `LP.cache` by Launchpad pages. */
export interface LaunchpadCache {
  /** API representation of the page's primary object. */
  context: LaunchpadCacheContext;
  /** Whether the current user can access the merge request action. */
  merge_request_enabled?: boolean;
  /** Additional values supplied by individual pages. */
  [name: string]: unknown;
}

/** The global Launchpad page data used by JavaScript modules. */
export interface LaunchpadGlobal {
  /** Data serialized into the page for JavaScript modules. */
  cache: LaunchpadCache;
}

/** Globals supplied by the YUI loader and Launchpad page bootstrap. */
declare global {
  /** Launchpad data serialized into the current page. */
  const LP: LaunchpadGlobal;
  /** Global YUI module loader. */
  const YUI: YuiStatic;
}
