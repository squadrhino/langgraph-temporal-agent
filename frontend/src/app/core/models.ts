/** Types mirroring the FastAPI schemas in `app/main.py`. */

export type OrderStatus =
  | 'pending'
  | 'paid'
  | 'processing'
  | 'shipped'
  | 'delivered'
  | 'cancelled'
  | 'refunded';

export type ProductCategory = 'laptop' | 'printer' | 'spare_part';

export interface Product {
  sku: string;
  name: string;
  category: ProductCategory;
  price: number;
}

export interface Order {
  id: string;
  customer_id: string;
  product: Product;
  quantity: number;
  total: number;
  status: OrderStatus;
  created_at: string;
}

export interface OrderCreate {
  customer_id: string;
  product_sku: string;
  quantity: number;
}

export interface AgentResponse {
  message: string;
}

export interface Health {
  status: string;
  orders: number;
  products: number;
}

/** Declared in the order a fulfilment team reads them, not alphabetically. */
export const ORDER_STATUSES: readonly OrderStatus[] = [
  'pending',
  'paid',
  'processing',
  'shipped',
  'delivered',
  'cancelled',
  'refunded',
] as const;

/** Statuses that still need someone to act on them. */
export const OPEN_STATUSES: readonly OrderStatus[] = ['pending', 'paid', 'processing'] as const;

/** Statuses that never became revenue. */
export const VOID_STATUSES: readonly OrderStatus[] = ['cancelled', 'refunded'] as const;

export const CATEGORY_LABELS: Record<ProductCategory, string> = {
  laptop: 'Laptop',
  printer: 'Printer',
  spare_part: 'Spare part',
};

export type SortKey = 'id' | 'customer_id' | 'product' | 'quantity' | 'total' | 'status' | 'created_at';

export interface SortState {
  key: SortKey;
  dir: 'asc' | 'desc';
}
