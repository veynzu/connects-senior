import { environment } from '../../environments/environment';

/**
 * URL base del backend (FastAPI).
 * - Desarrollo (ng serve, ng build --configuration development): http://127.0.0.1:8000, porque
 *   angular.json reemplaza environment.ts por environment.development.ts (fileReplacements).
 * - Producción (ng build): el backend desplegado en Cloud Run.
 */
export const API_BASE_URL = environment.apiBaseUrl;
