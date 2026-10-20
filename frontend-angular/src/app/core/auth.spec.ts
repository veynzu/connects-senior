import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { API_BASE_URL } from './api-config';
import { Auth, LoginResponse } from './auth';

/** Proceso 1: pruebas de contrato HTTP con backend simulado (sin usar Firebase real). */
describe('Auth (Proceso 1)', () => {
  let auth: Auth;
  let http: HttpTestingController;

  beforeEach(() => {
    localStorage.removeItem('connects-senior.sesion');
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    auth = TestBed.inject(Auth);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
    localStorage.removeItem('connects-senior.sesion');
  });

  it('envia correo y contrasena al endpoint de acceso y conserva la sesion de prueba', () => {
    const respuesta: LoginResponse = {
      id_token: 'token-ficticio',
      refresh_token: 'refresh-ficticio',
      expires_in: 3600,
      user: {
        uid: 'uid-ficticio',
        email: 'familiar@example.invalid',
        email_verified: true,
        display_name: 'Familiar Prueba',
        photo_url: null,
        role: 'familiar',
      },
    };

    auth.login('familiar@example.invalid', 'clave-solo-prueba').subscribe();
    const solicitud = http.expectOne(`${API_BASE_URL}/api/auth/login`);
    expect(solicitud.request.method).toBe('POST');
    expect(solicitud.request.body).toEqual({
      email: 'familiar@example.invalid', password: 'clave-solo-prueba',
    });
    solicitud.flush(respuesta);

    expect(auth.estaAutenticado()).toBe(true);
    expect(auth.rutaInicial()).toBe('/familiar');
  });

  it('no crea sesion ante credenciales rechazadas por el servidor', () => {
    let fueRechazado = false;
    auth.login('familiar@example.invalid', 'incorrecta').subscribe({
      error: () => { fueRechazado = true; },
    });
    const solicitud = http.expectOne(`${API_BASE_URL}/api/auth/login`);
    solicitud.flush({ detail: 'Credenciales incorrectas' }, {
      status: 401, statusText: 'Unauthorized',
    });
    expect(fueRechazado).toBe(true);
    expect(auth.estaAutenticado()).toBe(false);
  });

  it('solicita recuperacion de acceso por correo sin usar servicios reales', () => {
    let completada = false;
    auth.recuperar('familiar@example.invalid').subscribe(() => { completada = true; });
    const solicitud = http.expectOne(`${API_BASE_URL}/api/auth/password-reset`);
    expect(solicitud.request.method).toBe('POST');
    expect(solicitud.request.body).toEqual({ email: 'familiar@example.invalid' });
    solicitud.flush(null, { status: 204, statusText: 'No Content' });
    expect(completada).toBe(true);
  });
});
