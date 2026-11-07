export interface ModelPricing {
  currency: 'CNY' | 'USD'; input_per_million: string | number; output_per_million: string | number;
  cached_input_per_million?: string | number | null;
}
export function isPricing(value: unknown): value is ModelPricing {
  if (!value || typeof value !== 'object') return false;
  const p = value as Record<string, unknown>;
  const rate = (v: unknown) => (typeof v === 'number' || typeof v === 'string' && v.trim() !== '')
    && Number.isFinite(Number(v)) && Number(v) >= 0 && Number(v) <= 1000000;
  return ['CNY', 'USD'].includes(String(p.currency)) && rate(p.input_per_million) && rate(p.output_per_million)
    && (p.cached_input_per_million == null || rate(p.cached_input_per_million));
}
export interface ModelProfile {
  id: string; name: string; base_url: string; model: string;
  compatibility_profile: 'auto' | 'generic' | 'deepseek'; enabled: boolean; created_at: string;
  pricing?: ModelPricing | null;
}
export interface ModelCatalog {
  items: ModelProfile[];
}
export interface ModelProfileInput {
  id: string; name: string; base_url: string; model: string; api_key: string;
  compatibility_profile: ModelProfile['compatibility_profile'];
  pricing?: ModelPricing | null;
}
export function isModelProfile(value: unknown): value is ModelProfile {
  if (!value || typeof value !== 'object') return false;
  const item = value as Record<string, unknown>;
  return ['id', 'name', 'base_url', 'model', 'created_at'].every((key) => typeof item[key] === 'string')
    && ['auto', 'generic', 'deepseek'].includes(String(item.compatibility_profile))
    && typeof item.enabled === 'boolean' && !('api_key' in item) && !('encrypted_key' in item)
    && (item.pricing == null || isPricing(item.pricing));
}
export function isModelCatalog(value: unknown): value is ModelCatalog {
  if (!value || typeof value !== 'object') return false;
  const catalog = value as Record<string, unknown>;
  return Array.isArray(catalog.items) && catalog.items.length <= 100 && catalog.items.every(isModelProfile);
}
