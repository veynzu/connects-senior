import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { API_BASE_URL } from '../../core/api-config';
import { Alertas, SolicitudAyuda } from './alertas';

/** Proceso 3: verifica contratos HTTP; no envia alertas, push ni ubicaciones reales. */
describe('Alertas (Proceso 3)', () => {
  let servicio: Alertas;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    servicio = TestBed.inject(Alertas);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('permite solicitar ayuda sin ubicacion y sin incluir coordenadas en el cuerpo', () => {
    let recibida: SolicitudAyuda | undefined;
    servicio.crear({ category: 'ayuda_en_casa' }).subscribe((r) => { recibida = r; });
    const solicitud = http.expectOne(`${API_BASE_URL}/api/alerts`);
    expect(solicitud.request.method).toBe('POST');
    expect(solicitud.request.body).toEqual({ category: 'ayuda_en_casa' });
    expect(solicitud.request.body.latitude).toBeUndefined();
    expect(solicitud.request.body.longitude).toBeUndefined();
    solicitud.flush({
      id: '88', status: 'pendiente', created_at: '2026-10-19T12:00:00Z',
      location_available: false, has_trusted_contacts: true,
    } satisfies SolicitudAyuda);
    expect(recibida).toMatchObject({ id: '88', location_available: false });
  });

  it('incluye la ubicacion cuando el usuario la autorizo', () => {
    servicio.crear({ category: 'problema', latitude: 4.65, longitude: -74.1 }).subscribe();
    const solicitud = http.expectOne(`${API_BASE_URL}/api/alerts`);
    expect(solicitud.request.body).toEqual({
      category: 'problema', latitude: 4.65, longitude: -74.1,
    });
    solicitud.flush({ id: '89', status: 'pendiente', created_at: '2026-10-19T12:00:00Z',
      location_available: true, has_trusted_contacts: true });
  });

  it('confirma la atencion enviando la solicitud al endpoint correspondiente', () => {
    servicio.atender('88').subscribe();
    const solicitud = http.expectOne(`${API_BASE_URL}/api/alerts/88/acknowledge`);
    expect(solicitud.request.method).toBe('POST');
    expect(solicitud.request.body).toEqual({});
    solicitud.flush({ id: '88', adult_name: 'Persona Prueba', category: 'ayuda_en_casa',
      status: 'en_atencion', created_at: '2026-10-19T12:00:00Z', location_available: false });
  });

  it('actualiza el estado sin reemplazar la solicitud completa', () => {
    servicio.cambiarEstado('88', 'atendida').subscribe();
    const solicitud = http.expectOne(`${API_BASE_URL}/api/alerts/88`);
    expect(solicitud.request.method).toBe('PATCH');
    expect(solicitud.request.body).toEqual({ status: 'atendida' });
    solicitud.flush({ id: '88', adult_name: 'Persona Prueba', category: 'ayuda_en_casa',
      status: 'atendida', created_at: '2026-10-19T12:00:00Z', location_available: false });
  });
});
