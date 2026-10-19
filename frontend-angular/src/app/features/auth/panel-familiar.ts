import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import { Adulto, Adultos } from './adultos';

type Estado = 'cargando' | 'error' | 'listo';

@Component({
  selector: 'app-panel-familiar',
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: '../../shared/formulario.css',
  template: `
    <main class="pagina">
      <h1>Hola, {{ nombre() }}</h1>
      <p>{{ esFamiliar() ? 'Tus adultos mayores' : 'Adultos mayores a tu cargo' }}</p>

      @switch (estado()) {
        @case ('cargando') {
          <p role="status">Cargando…</p>
        }
        @case ('error') {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
          <button type="button" class="entrar secundario" (click)="cargar()">
            Intentar de nuevo
          </button>
        }
        @case ('listo') {
          @if (lista().length === 0) {
            <p>
              {{
                esFamiliar()
                  ? 'Todavía no has registrado a ningún adulto mayor.'
                  : 'Todavía no tienes adultos mayores a tu cargo.'
              }}
            </p>
          } @else {
            <ul class="lista">
              @for (a of lista(); track a.id) {
                <li class="tarjeta">
                  <p class="nombre">{{ a.first_name }} {{ a.last_name }}</p>
                  <a class="enlace" [routerLink]="['/adultos', a.id, 'contactos']">
                    Ver contactos
                  </a>
                  <a class="enlace" [routerLink]="['/adultos', a.id, 'favoritos']">
                    Contactos favoritos
                  </a>
                  <a class="enlace" [routerLink]="['/adultos', a.id, 'solicitudes']">
                    Ver solicitudes
                  </a>
                  <a class="enlace" [routerLink]="['/adultos', a.id, 'recordatorios']">
                    Ver recordatorios
                  </a>
                  <a class="enlace" [routerLink]="['/adultos', a.id, 'historial']">
                    Historial de llamadas
                  </a>
                  @if (esFamiliar()) {
                    <a class="enlace" [routerLink]="['/adultos', a.id, 'cuidadores', 'nuevo']">
                      Invitar cuidador
                    </a>
                  }
                </li>
              }
            </ul>
          }
        }
      }

      @if (esFamiliar()) {
        <a class="entrar" routerLink="/adultos/nuevo">Registrar adulto mayor</a>
      }
      <a class="entrar secundario" routerLink="/perfil">Mi perfil</a>
      <button type="button" class="entrar secundario" (click)="salir()">Cerrar sesión</button>
    </main>
  `,
})
export class PanelFamiliar {
  private readonly auth = inject(Auth);
  private readonly adultos = inject(Adultos);

  protected readonly estado = signal<Estado>('cargando');
  protected readonly lista = signal<Adulto[]>([]);
  protected readonly mensajeError = signal('');

  /** Contrato A9: invitar cuidadores es solo del familiar responsable; el cuidador no ve ese botón. */
  protected readonly esFamiliar = computed(() => this.auth.usuario()?.role !== 'cuidador');

  protected readonly nombre = computed(() => {
    const usuario = this.auth.usuario();
    const completo = usuario?.display_name || usuario?.email || '';
    return completo.split(' ')[0];
  });

  constructor() {
    this.cargar();
  }

  protected cargar(): void {
    this.estado.set('cargando');
    this.adultos.listar().subscribe({
      next: (adultos) => {
        this.lista.set(adultos);
        this.estado.set('listo');
      },
      error: (error: HttpErrorResponse) => {
        if (error.status === 401) {
          this.auth.cerrarSesion();
          return;
        }
        this.mensajeError.set(
          error.status === 403
            ? 'No tienes permiso para ver esta información.'
            : 'No pudimos cargar los adultos mayores. Revisa tu conexión e inténtalo de nuevo.',
        );
        this.estado.set('error');
      },
    });
  }

  protected salir(): void {
    this.auth.cerrarSesion();
  }
}