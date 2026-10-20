import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { API_BASE_URL } from '../../core/api-config';
import { Contactos, Contacto, NuevoContacto } from './contactos';

/** Proceso 2: verifica contratos y aislamiento de consultas por adulto; no usa Neon. */
describe('Contactos (Proceso 2)', () => {
  let servicio: Contactos;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    servicio = TestBed.inject(Contactos);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('consulta los contactos sin adult_id cuando se deducen del usuario autenticado', () => {
    let resultado: Contacto[] | undefined;
    servicio.listar().subscribe((contactos) => { resultado = contactos; });
    const solicitud = http.expectOne(`${API_BASE_URL}/api/contacts`);
    expect(solicitud.request.method).toBe('GET');
    expect(solicitud.request.params.has('adult_id')).toBe(false);
    solicitud.flush([]);
    expect(resultado).toEqual([]);
  });

  it('incluye adult_id al consultar contactos de un adulto asociado', () => {
    servicio.listar('77').subscribe();
    const solicitud = http.expectOne((request) => request.url === `${API_BASE_URL}/api/contacts`);
    expect(solicitud.request.method).toBe('GET');
    expect(solicitud.request.params.get('adult_id')).toBe('77');
    solicitud.flush([]);
  });

  it('conserva los permisos de confianza al registrar un contacto', () => {
    const datos: NuevoContacto = {
      adult_id: '77', first_name: 'Contacto', last_name: 'De Prueba', relationship: 'Familiar',
      phone: null, email: 'contacto@example.invalid',
      permissions: { calls: true, video_calls: true, messages: true, help_alerts: true },
    };
    servicio.crear(datos).subscribe();
    const solicitud = http.expectOne(`${API_BASE_URL}/api/contacts`);
    expect(solicitud.request.method).toBe('POST');
    expect(solicitud.request.body).toEqual(datos);
    // El servidor esta simulado; no verifica la persistencia real.
    solicitud.flush({ id: '11', ...datos, is_favorite: false, favorite_order: null, photo_url: null });
  });

  it('actualiza un contacto mediante PATCH en vez de crear otro', () => {
    servicio.actualizar('11', { relationship: 'Cuidador' }).subscribe();
    const solicitud = http.expectOne(`${API_BASE_URL}/api/contacts/11`);
    expect(solicitud.request.method).toBe('PATCH');
    expect(solicitud.request.body).toEqual({ relationship: 'Cuidador' });
    solicitud.flush({ id: '11' });
  });
});
