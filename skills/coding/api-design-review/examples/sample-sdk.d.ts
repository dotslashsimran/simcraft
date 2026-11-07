// Deliberately flawed TypeScript SDK surface for testing sdk_surface.py.
// It belongs to the fictional "Bookshop Orders" API in this folder.

/** Options for creating a client. */
export interface ClientOptions {
  apiKey: string;
  baseUrl?: string;
  /** Milliseconds. There is no default, so requests can hang forever. */
  timeout?: number;
  maxRetries?: number;
}

/** Entry point for the SDK. */
export declare class BookshopClient {
  constructor(apiKey: string, baseUrl?: string, timeout?: number, retries?: number, debug?: boolean);
  readonly orders: OrdersResource;
  customers: any;
  /** Raw request escape hatch. */
  request<T = unknown>(method: string, path: string, body?: unknown): Promise<T>;
  private signRequest(payload: string): string;
}

export declare class OrdersResource {
  /** List orders. Returns every order in one call. */
  list(status?: string, expand: boolean): Promise<Order[]>;
  get(id: string): Promise<Order>;
  create(customerId: number, items: OrderItem[], notes?: string, sendEmail?: boolean, coupon?: string): Promise<Order>;
  cancel(id: string, callback: (err: Error | null, order?: Order) => void): void;
  updateOrder(id: string, data: any): Promise<any>;
}

export interface Order {
  id: string;
  customer_id: number;
  status: "pending" | "Shipped" | "CANCELLED";
  total: number;
  createdAt: string;
}

export interface OrderItem {
  isbn: string;
  quantity: number;
}

export type WebhookHandler = (event: any) => void;

export declare enum OrderStatus {
  Pending = "pending",
  Shipped = "Shipped",
  Cancelled = "CANCELLED"
}

/** Thrown for any non-2xx response. */
export declare class ApiError extends Error {
  status: number;
  message: string;
}

export declare class RateLimitError {
  retryAfter: number;
}

export declare function verifyWebhook(payload: string, header: string, secret: string, tolerance?: number): boolean;

export * from "./pagination";
