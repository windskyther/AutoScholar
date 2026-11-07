export interface ModelProfile {
  id: string; name: string; base_url: string; model: string;
  compatibility_profile: 'auto' | 'generic' | 'deepseek'; enabled: boolean; created_at: string;
}
export interface ModelCatalog {
  items: ModelProfile[];
}
export interface ModelProfileInput {
  id: string; name: string; base_url: string; model: string; api_key: string;
  compatibility_profile: ModelProfile['compatibility_profile'];
}
export function isModelProfile(value: unknown): value is ModelProfile {
  if (!value || typeof value !== 'object') return false;
  const item = value as Record<string, unknown>;
  return ['id', 'name', 'base_url', 'model', 'created_at'].every((key) => typeof item[key] === 'string')
    && ['auto', 'generic', 'deepseek'].includes(String(item.compatibility_profile))
    && typeof item.enabled === 'boolean' && !('api_key' in item) && !('encrypted_key' in item);
}
export function isModelCatalog(value: unknown): value is ModelCatalog {
  if (!value || typeof value !== 'object') return false;
  const catalog = value as Record<string, unknown>;
  return Array.isArray(catalog.items) && catalog.items.length <= 100 && catalog.items.every(isModelProfile);
}
