import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { Auth } from '../../core/auth';
import {
    DatosRecordatorio,
    ETIQUETAS_REPETICION,
    ETIQUETAS_TIPO,
    Recordatorio,
    Recordatorios,
    Repeticion,
    TipoRecordatorio,
} from './recordatorios';

type Estado = 'cargando' | 'error' | 'listo';

/** Convierte una fecha ISO al formato que entiende <input type="datetime-local">. */
function aFechaLocal(iso: string): string {
    const f = new Date(iso);
    const dos = (n: number) => String(n).padStart(2, '0');
    return `${f.getFullYear()}-${dos(f.getMonth() + 1)}-${dos(f.getDate())}T${dos(f.getHours())}:${dos(f.getMinutes())}`;
}

@Component({
    selector: 'app-formulario-recordatorio',
    imports: [ReactiveFormsModule, RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    styleUrls: ['../../shared/formulario.css'],
    styles: [
        `
      .campo {
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
        margin-block-end: 1rem;
      }
      .campo label {
        font-weight: 600;
      }
      .campo input,
      .campo select {
        min-height: var(--touch-target, 3.5rem);
        padding: 0 1rem;
        font: inherit;
        border: 2px solid #5f6368;
        border-radius: 0.75rem;
      }
      .campo .error {
        color: var(--color-danger, #e22931);
      }
    `,
    ],
    template: `
    <main class="pagina">
      <h1>{{ editando ? 'Editar recordatorio' : 'Agregar recordatorio' }}</h1>

      @switch (estado()) {
        @case ('cargando') {
          <p role="status">Cargando…</p>
        }
        @case ('error') {
          <p class="error-general" role="alert">{{ mensajeError() }}</p>
        }
        @case ('listo') {
          <form [formGroup]="formulario" (ngSubmit)="enviar()" novalidate>
        <p class="nota"><span class="obligatorio" aria-hidden="true">*</span> Campo obligatorio</p>
            <div class="campo">
              <label for="tipo">Tipo <span class="obligatorio" aria-hidden="true">*</span></label>
              <select id="tipo" aria-required="true" formControlName="kind">
                @for (t of tipos; track t.valor) {
                  <option [value]="t.valor">{{ t.texto }}</option>
                }
              </select>
            </div>

            <div class="campo">
              <label for="descripcion">Descripción <span class="obligatorio" aria-hidden="true">*</span></label>
              <input
                id="descripcion" aria-required="true"
                type="text"
                formControlName="description"
                [attr.aria-invalid]="invalido('description')"
                aria-describedby="error-descripcion"
              />
              @if (invalido('description')) {
                <span class="error" id="error-descripcion" role="alert">
                  Escribe una descripción.
                </span>
              }
            </div>

            <div class="campo">
              <label for="fecha">Fecha y hora <span class="obligatorio" aria-hidden="true">*</span></label>
              <input
                id="fecha" aria-required="true"
                type="datetime-local"
                formControlName="fecha"
                [attr.aria-invalid]="invalido('fecha')"
                aria-describedby="error-fecha"
              />
              @if (invalido('fecha')) {
                <span class="error" id="error-fecha" role="alert">Elige la fecha y la hora.</span>
              }
            </div>

            <div class="campo">
              <label for="repeticion">Repetición</label>
              <select id="repeticion" formControlName="repeat">
                @for (r of repeticiones; track r.valor) {
                  <option [value]="r.valor">{{ r.texto }}</option>
                }
              </select>
            </div>

            @if (errorEnvio()) {
              <p class="error-general" role="alert">{{ errorEnvio() }}</p>
            }

            <button type="submit" class="entrar" [disabled]="enviando()">
              {{ enviando() ? 'Guardando…' : 'Guardar' }}
            </button>
          </form>
        }
      }

      <a class="enlace" [routerLink]="['/adultos', adultoId, 'recordatorios']">Cancelar</a>
    </main>
  `,
})
export class FormularioRecordatorio {
    private readonly ruta = inject(ActivatedRoute);
    private readonly router = inject(Router);
    private readonly servicio = inject(Recordatorios);
    private readonly auth = inject(Auth);

    protected readonly adultoId = this.ruta.snapshot.paramMap.get('adultoId') ?? '';
    private readonly id = this.ruta.snapshot.paramMap.get('id');
    protected readonly editando = this.id !== null;

    protected readonly tipos: readonly { valor: TipoRecordatorio; texto: string }[] = [
        { valor: 'medicamento', texto: ETIQUETAS_TIPO.medicamento },
        { valor: 'cita_medica', texto: ETIQUETAS_TIPO.cita_medica },
    ];

    protected readonly repeticiones: readonly { valor: Repeticion; texto: string }[] = [
        { valor: 'ninguna', texto: ETIQUETAS_REPETICION.ninguna },
        { valor: 'diaria', texto: ETIQUETAS_REPETICION.diaria },
        { valor: 'semanal', texto: ETIQUETAS_REPETICION.semanal },
    ];

    protected readonly estado = signal<Estado>(this.editando ? 'cargando' : 'listo');
    protected readonly mensajeError = signal('');
    protected readonly enviando = signal(false);
    protected readonly errorEnvio = signal('');

    protected readonly formulario = new FormGroup({
        kind: new FormControl<TipoRecordatorio>('medicamento', { nonNullable: true }),
        description: new FormControl('', {
            nonNullable: true,
            validators: [Validators.required, Validators.pattern(/\S/)],
        }),
        fecha: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
        repeat: new FormControl<Repeticion>('ninguna', { nonNullable: true }),
    });

    constructor() {
        if (this.editando) {
            this.cargar();
        }
    }

    private cargar(): void {
        this.estado.set('cargando');
        this.servicio.listar(this.adultoId).subscribe({
            next: (lista) => {
                const r: Recordatorio | undefined = lista.find((x) => x.id === this.id);
                if (!r) {
                    this.mensajeError.set('No encontramos este recordatorio.');
                    this.estado.set('error');
                    return;
                }
                this.formulario.setValue({
                    kind: r.kind,
                    description: r.description,
                    fecha: aFechaLocal(r.scheduled_at),
                    repeat: r.repeat,
                });
                this.estado.set('listo');
            },
            error: (error: HttpErrorResponse) => {
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.mensajeError.set(
                    'No pudimos cargar el recordatorio. Revisa tu conexión e inténtalo de nuevo.',
                );
                this.estado.set('error');
            },
        });
    }

    protected invalido(campo: 'description' | 'fecha'): boolean {
        const c = this.formulario.controls[campo];
        return c.invalid && (c.touched || c.dirty);
    }

    protected enviar(): void {
        if (this.formulario.invalid) {
            this.formulario.markAllAsTouched();
            return;
        }
        const v = this.formulario.getRawValue();
        const datos: DatosRecordatorio = {
            kind: v.kind,
            description: v.description.trim(),
            scheduled_at: new Date(v.fecha).toISOString(),
            repeat: v.repeat,
        };

        this.enviando.set(true);
        this.errorEnvio.set('');
        const peticion =
            this.editando && this.id
                ? this.servicio.actualizar(this.id, datos)
                : this.servicio.crear(datos, this.adultoId);

        peticion.subscribe({
            next: () => {
                void this.router.navigate(['/adultos', this.adultoId, 'recordatorios']);
            },
            error: (error: HttpErrorResponse) => {
                this.enviando.set(false);
                if (error.status === 401) {
                    this.auth.cerrarSesion();
                    return;
                }
                this.errorEnvio.set(
                    error.status === 403
                        ? 'No tienes permiso para guardar recordatorios de este adulto mayor.'
                        : error.status === 422
                            ? 'Revisa los datos del recordatorio.'
                            : 'No pudimos guardar el recordatorio. Inténtalo de nuevo.',
                );
            },
        });
    }
}