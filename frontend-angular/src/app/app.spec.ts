import { provideHttpClient } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { App } from './app';

/**
 * Pruebas del componente raiz de Connects Senior.
 * No se comprueba el texto de la plantilla inicial de Angular ('Hello, frontend-angular'),
 * porque la interfaz real renderiza pantallas a traves de router-outlet.
 */
describe('App', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [App],
      // Auth y los componentes compartidos usan HttpClient.
      providers: [provideHttpClient()],
    }).compileComponents();
  });

  it('should create the app', () => {
    const fixture = TestBed.createComponent(App);
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('should render the router outlet', async () => {
    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();
    await fixture.whenStable();

    const compiled: HTMLElement = fixture.nativeElement;
    expect(compiled.querySelector('router-outlet')).not.toBeNull();
  });
});
