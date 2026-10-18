import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import {
    ChangeDetectionStrategy,
    Component,
    Injectable,
    computed,
    inject,
    signal,
} from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { Observable, forkJoin } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';
import { Auth } from '../../core/auth';

/*
 * Contrato v1.0, C1 (GET /api/contacts?adult_id=) y C3 (PATCH /api/contacts/{id}
 * con is_favorite y favorite_order). Estos endpoints todavía NO existen en el backend.
 */
export interface ContactoFavorito {
    id: string;
    first_name: string;
    last_name: string;
    relationship: string;
    is_favorite: boolean;
    favorite_order: number | null;
}

interface CambioFavorito {
    is_favorite?: boolean;
    favorite_order?: number;
}

@Injectable({ providedIn: 'root' })
export class Favoritos {
    private readonly http = inject(HttpClient);

    listar(adultId: string): Observable<ContactoFavorito[]> {
        return this.http.get<ContactoFavorito[]>(`${API_BASE_URL}/api/contacts`, {
            params: { adult_id: adultId },
        });
    }

    cambiar(id: string, cambio: CambioFavorito): Observable<unknown> {
        return this.http.patch<unknown>(`${API_BASE_URL}/api/contacts/${id}`, cambio);
    }
}

type Estado = 'cargando' | 'error' | 'listo';

@Component({
    selector: 'app-favoritos-adulto',
    imports: [RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrl: '../../shared/formulario.css',
    template: `
    <main class="pagina">
      <h1>Contactos favoritos</h1>
      <p>Los favoritos aparecen primero en la lista del adulto mayor.</p>

      @switch (estado()) {
        @case ('cargando') {
          <p role="status">Cargando contactos…</p>
        }
        @case ('error') {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
          <button type="button" class="entrar secundario" (click)="cargar()">
            Intentar de nuevo
          </button>
        }
        @case ('listo') {
          <h2>Favoritos</h2>
          @if (favoritos().length === 0) {
            <p>Todavía no hay favoritos.</p>
          } @else {
            <ul class="lista">
              @for (c of favoritos(); track c.id; let i = $index) {
                <li class="tarjeta">
                  <p class="nombre">{{ i + 1 }}. {{ c.first_name }} {{ c.last_name }}</p>
                  <p class="relacion">{{ c.relationship }}</p>
                  <button
                    type="button"
                    class="entrar secundario"
                    [disabled]="ocupado() || i === 0"
                    [attr.aria-label]="'Subir a ' + c.first_name + ' ' + c.last_name"
                    (click)="mover(i, -1)"
                  >
                    Subir
                  </button>
                  <button
                    type="button"
                    class="entrar secundario"
                    [disabled]="ocupado() || i === favoritos().length - 1"
                    [attr.aria-label]="'Bajar a ' + c.first_name + ' ' + c.last_name"
                    (click)="mover(i, 1)"
                  >
                    Bajar
                  </button>
                  <button
                    type="button"
                    class="entrar secundario"
                    [disabled]="ocupado()"
                    [attr.aria-label]="'Quitar de favoritos a ' + c.first_name + ' ' + c.last_name"
                    (click)="quitar(c)"
                  >
                    Quitar de favoritos
                  </button>
                </li>
              }
            </ul>
          }

          <h2>Otros contactos</h2>
          @if (otros().length === 0) {
            <p>No hay más contactos.</p>
          } @else {
            <ul class="lista">
              @for (c of otros(); track c.id) {
                <li class="tarjeta">
                  <p class="nombre">{{ c.first_name }} {{ c.last_name }}</p>
                  <p class="relacion">{{ c.relationship }}</p>
                  <button
                    type="button"
                    class="entrar"
                    [disabled]="ocupado()"
                    [attr.aria-label]="'Marcar como favorito a ' + c.first_name + ' ' + c.last_name"
                    (click)="marcar(c)"
                  >
                    Marcar como favorito
                  </button>
                </li>
              }
            </ul>
          }

          @if (errorAccion()) {
            <p class="error-general" role="alert">{{ errorAccion() }}</p>
          }
        }
      }

      <a class="enlace" routerLink="/familiar">Volver</a>
    </main>
  `,
})
export class FavoritosAdulto {
    private readonly ruta = inject(ActivatedRoute);
    private readonly servicio = inject(Favoritos);
    private readonly auth = inject(Auth);

    private readonly adultoId = this.ruta.snapshot.paramMap.get('adultoId') ?? '';

    protected readonly estado = signal<Estado>('cargando');
    protected readonly lista = signal<ContactoFavorito[]>([]);
    protected readonly mensajeError = signal('');
    protected readonly ocupado = signal(false);
    protected readonly errorAccion = signal('');

    protected readonly favoritos = computed(() =>
        this.lista()
            .filter((c) => c.is_favorite)
            .sort(
                (a, b) =>
                    (a.favorite_order ?? Number.MAX_SAFE_INTEGER) -
                    (b.favorite_order ?? Number.MAX_SAFE_INTEGER),
            ),
    );

    protected readonly otros = computed(() =>
        this.lista()
            .filter((c) => !c.is_favorite)
            .sort((a, b) => `${a.first_name} ${a.last_name}`.localeCompare(`${b.first_name} ${b.last_name}`, 'es')),
    );

    constructor() {
        this.cargar();
    }

    protected cargar(): void {
        this.estado.set('cargando');
        this.servicio.listar(this.adultoId).subscribe({
            next: (contactos) => {
                this.lista.set(contactos);
                this.estado.set('listo');
            },
            error: (error: HttpErrorResponse) => {
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.mensajeError.set(
                    error.status === 403
                        ? 'No tienes permiso para ver los contactos de este adulto mayor.'
                        : 'No pudimos cargar los contactos. Revisa tu conexión e inténtalo de nuevo.',
                );
                this.estado.set('error');
            },
        });
    }

    protected marcar(c: ContactoFavorito): void {
        const orden = this.favoritos().length + 1;
        this.ejecutar(
            [this.servicio.cambiar(c.id, { is_favorite: true, favorite_order: orden })],
            () =>
                this.lista.update((l) =>
                    l.map((x) => (x.id === c.id ? { ...x, is_favorite: true, favorite_order: orden } : x)),
                ),
        );
    }

    protected quitar(c: ContactoFavorito): void {
        this.ejecutar([this.servicio.cambiar(c.id, { is_favorite: false })], () =>
            this.lista.update((l) =>
                l.map((x) => (x.id === c.id ? { ...x, is_favorite: false, favorite_order: null } : x)),
            ),
        );
    }

    protected mover(indice: number, delta: -1 | 1): void {
        const orden = [...this.favoritos()];
        const destino = indice + delta;
        if (destino < 0 || destino >= orden.length) {
            return;
        }
        [orden[indice], orden[destino]] = [orden[destino], orden[indice]];

        // Se numera 1, 2, 3… y solo se envían los que cambian de número.
        const nuevos = orden.map((c, i) => ({ id: c.id, favorite_order: i + 1 }));
        const cambios = nuevos.filter(
            (n) => this.lista().find((c) => c.id === n.id)?.favorite_order !== n.favorite_order,
        );
        this.ejecutar(
            cambios.map((n) => this.servicio.cambiar(n.id, { favorite_order: n.favorite_order })),
            () =>
                this.lista.update((l) =>
                    l.map((x) => {
                        const n = nuevos.find((y) => y.id === x.id);
                        return n ? { ...x, favorite_order: n.favorite_order } : x;
                    }),
                ),
        );
    }

    private ejecutar(peticiones: Observable<unknown>[], alExito: () => void): void {
        if (peticiones.length === 0) {
            return;
        }
        this.ocupado.set(true);
        this.errorAccion.set('');
        forkJoin(peticiones).subscribe({
            next: () => {
                alExito();
                this.ocupado.set(false);
            },
            error: (error: HttpErrorResponse) => {
                this.ocupado.set(false);
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.errorAccion.set(
                    error.status === 403
                        ? 'No tienes permiso para cambiar los contactos de este adulto mayor.'
                        : 'No pudimos guardar el cambio. Inténtalo de nuevo.',
                );
            },
        });
    }
}