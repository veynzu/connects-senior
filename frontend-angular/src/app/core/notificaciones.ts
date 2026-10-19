import { Injectable, Injector, computed, effect, inject, signal, untracked } from '@angular/core';
import { Observable, Subscription, catchError, forkJoin, map, of, switchMap } from 'rxjs';

import { Adultos } from '../features/auth/adultos';
import {
  Alertas,
  ETIQUETAS_CATEGORIA,
  ETIQUETAS_ESTADO,
  Solicitud,
} from '../features/ayuda/alertas';
import { HistorialLlamadas } from '../features/comunicacion/historial';
import { Contacto, Contactos } from '../features/contactos/contactos';
import { MensajesVoz } from '../features/mensajes/mensajes-voz';
import { Recordatorios } from '../features/recordatorios/recordatorios';
import { Auth, Rol } from './auth';

/*
 * Campana de notificaciones: reúne lo que ya está guardado en el backend (pedidos de ayuda,
 * recordatorios, mensajes de voz y llamadas no contestadas). No son notificaciones push
 * (A10): se consulta cada minuto mientras haya sesión y cada vez que se abre el panel.
 *
 * Los mensajes de texto (M2) no se incluyen: el backend no marca mensajes leídos o no
 * leídos y revisarlos exigiría una consulta por cada contacto.
 */

export type TipoNotificacion = 'alerta' | 'recordatorio' | 'mensaje_voz' | 'llamada';

export interface Notificacion {
  /** `tipo:id` del recurso; con este valor se recuerda cuáles ya se vieron. */
  id: string;
  tipo: TipoNotificacion;
  titulo: string;
  detalle: string;
  /** Fecha ISO 8601: ordena la lista y da la hora relativa. */
  fecha: string;
  ruta: string;
  adultoId?: string;
}

type Fuente = 'alertas' | 'voz' | 'recordatorios' | 'llamadas';
type PorFuente = Record<Fuente, Notificacion[]>;
/** null = la consulta falló y se conserva lo anterior de esa fuente. */
type Resultado = [Fuente, Notificacion[] | null];

interface SesionActiva {
  uid: string;
  rol: Rol | null;
}

const SIN_NOTIFICACIONES: PorFuente = { alertas: [], voz: [], recordatorios: [], llamadas: [] };

const MINUTO_MS = 60_000;
const HORA_MS = 60 * MINUTO_MS;
const DIA_MS = 24 * HORA_MS;
const INTERVALO_MS = MINUTO_MS;
/** Máximo de notificaciones vistas que se recuerdan por persona. */
const MAXIMO_VISTAS = 500;

function tiempo(iso: string): number {
  const valor = Date.parse(iso);
  return Number.isNaN(valor) ? 0 : valor;
}

function horaCorta(fecha: Date): string {
  return fecha.toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
}

function fechaCorta(fecha: Date): string {
  return fecha.toLocaleDateString('es-CO', { day: 'numeric', month: 'short' });
}

/** "Hoy a las 08:30 a. m.", "Ayer a las …", "Mañana a las …" o la fecha corta. */
function diaYHora(iso: string): string {
  const fecha = new Date(iso);
  const hoy = new Date();
  hoy.setHours(0, 0, 0, 0);
  const dia = new Date(fecha);
  dia.setHours(0, 0, 0, 0);
  const diferencia = Math.round((dia.getTime() - hoy.getTime()) / DIA_MS);
  const nombre =
    diferencia === 0
      ? 'Hoy'
      : diferencia === -1
        ? 'Ayer'
        : diferencia === 1
          ? 'Mañana'
          : fechaCorta(fecha);
  return `${nombre} a las ${horaCorta(fecha)}`;
}

/** "Ahora", "hace 5 min", "hace 2 h", "en 20 min" o la fecha corta. */
export function horaRelativa(iso: string, ahora = Date.now()): string {
  const valor = Date.parse(iso);
  if (Number.isNaN(valor)) {
    return '';
  }
  const diferencia = ahora - valor;
  if (diferencia < -30_000) {
    const faltan = Math.ceil(-diferencia / MINUTO_MS);
    return faltan <= 60 ? `en ${faltan} min` : diaYHora(iso);
  }
  if (diferencia < MINUTO_MS) {
    return 'Ahora';
  }
  if (diferencia < HORA_MS) {
    return `hace ${Math.floor(diferencia / MINUTO_MS)} min`;
  }
  if (diferencia < DIA_MS) {
    return `hace ${Math.floor(diferencia / HORA_MS)} h`;
  }
  return fechaCorta(new Date(valor));
}

function claveVistas(uid: string): string {
  return `cs.notificaciones.vistas.${uid}`;
}

function alertaANotificacion(s: Solicitud, adultoId: string): Notificacion {
  // Mismos textos que la pantalla de solicitudes (ETIQUETAS_* de alertas.ts).
  const categoria = (s.category && ETIQUETAS_CATEGORIA[s.category]) || 'Sin categoría';
  const estado = ETIQUETAS_ESTADO[s.status] ?? s.status;
  return {
    id: `alerta:${s.id}`,
    tipo: 'alerta',
    titulo: `Pedido de ayuda de ${s.adult_name}`,
    detalle: `${categoria} · ${estado}`,
    fecha: s.created_at,
    ruta: `/adultos/${encodeURIComponent(adultoId)}/solicitudes/${encodeURIComponent(s.id)}`,
    adultoId,
  };
}

@Injectable({ providedIn: 'root' })
export class Notificaciones {
  private readonly auth = inject(Auth);
  private readonly injector = inject(Injector);
  private readonly adultos = inject(Adultos);
  private readonly alertas = inject(Alertas);
  private readonly voz = inject(MensajesVoz);
  private readonly recordatorios = inject(Recordatorios);
  private readonly llamadas = inject(HistorialLlamadas);
  private readonly contactos = inject(Contactos);

  private readonly porFuente = signal<PorFuente>(SIN_NOTIFICACIONES);
  private readonly vistas = signal<readonly string[]>([]);
  private readonly conjuntoVistas = computed(() => new Set(this.vistas()));

  /** Todas las notificaciones, de la más reciente a la más antigua. */
  readonly items = computed(() =>
    Object.values(this.porFuente())
      .flat()
      .sort((a, b) => tiempo(b.fecha) - tiempo(a.fecha)),
  );

  readonly noLeidas = computed(() => {
    const vistas = this.conjuntoVistas();
    return this.items().filter((n) => !vistas.has(n.id)).length;
  });

  /** Cambia solo cuando entra otra persona o cambia su rol (no con cada renovación del token). */
  private readonly sesion = computed<SesionActiva | null>(
    () => {
      const usuario = this.auth.usuario();
      return usuario ? { uid: usuario.uid, rol: usuario.role ?? null } : null;
    },
    { equal: (a, b) => a?.uid === b?.uid && a?.rol === b?.rol },
  );

  private uid: string | null = null;
  private consulta: Subscription | null = null;
  private iniciado = false;

  /** Consulta al entrar y luego cada minuto mientras haya sesión; se detiene al cerrarla. */
  iniciar(): void {
    if (this.iniciado) {
      return;
    }
    this.iniciado = true;
    effect(
      (alLimpiar) => {
        const sesion = this.sesion();
        untracked(() => {
          this.reiniciar(sesion?.uid ?? null);
          if (sesion) {
            this.actualizar();
          }
        });
        if (!sesion) {
          return;
        }
        const intervalo = setInterval(() => this.actualizar(), INTERVALO_MS);
        alLimpiar(() => {
          clearInterval(intervalo);
          this.cancelarConsulta();
        });
      },
      { injector: this.injector },
    );
  }

  /** Pide de nuevo las fuentes que corresponden al rol. Si una falla, conserva lo anterior. */
  actualizar(): void {
    // No se usa estaAutenticado(): borra la sesión cuando el id_token vence. Con el token
    // vencido, el interceptor lo renueva (A3) o cierra la sesión si la renovación falla.
    if (!this.uid || !this.auth.usuario()) {
      return;
    }
    const consultas = this.consultasPara(this.auth.usuario()?.role ?? null);
    this.cancelarConsulta();
    this.consulta = forkJoin(consultas).subscribe((resultados) => {
      this.porFuente.update((actual) => {
        const nuevo = { ...actual };
        for (const [fuente, lista] of resultados) {
          if (lista) {
            nuevo[fuente] = lista;
          }
        }
        return nuevo;
      });
    });
  }

  marcarTodasLeidas(): void {
    this.guardarVistas(this.items().map((n) => n.id));
  }

  marcarLeida(id: string): void {
    this.guardarVistas([id]);
  }

  esNoLeida(id: string): boolean {
    return !this.conjuntoVistas().has(id);
  }

  private consultasPara(rol: Rol | null): Observable<Resultado>[] {
    const consultas: Observable<Resultado>[] = [];
    const agregar = (fuente: Fuente, origen: Observable<Notificacion[]>) => {
      consultas.push(
        origen.pipe(
          catchError(() => of(null)),
          map((lista): Resultado => [fuente, lista]),
        ),
      );
    };

    if (rol === 'familiar' || rol === 'cuidador') {
      agregar('alertas', this.consultarAlertas());
      agregar('voz', this.consultarVoz());
    } else if (rol === 'adulto_mayor') {
      agregar('voz', this.consultarVoz());
      agregar('recordatorios', this.consultarRecordatorios());
      agregar('llamadas', this.consultarLlamadas());
    }
    // administrador (o sin rol): no hay fuentes; la campana muestra "Sin notificaciones".
    return consultas;
  }

  /** A8 y luego H2 por cada adulto: solicitudes pendientes o en atención. */
  private consultarAlertas(): Observable<Notificacion[]> {
    return this.adultos.listar().pipe(
      switchMap((adultos) =>
        adultos.length === 0
          ? of<Notificacion[]>([])
          : forkJoin(
              adultos.map((adulto) =>
                this.alertas.listar(adulto.id).pipe(
                  map((lista) =>
                    lista
                      .filter((s) => s.status === 'pendiente' || s.status === 'en_atencion')
                      .map((s) => alertaANotificacion(s, adulto.id)),
                  ),
                  // Si falla un adulto, se conservan sus avisos anteriores.
                  catchError(() =>
                    of(this.porFuente().alertas.filter((n) => n.adultoId === adulto.id)),
                  ),
                ),
              ),
            ).pipe(map((listas) => listas.flat())),
      ),
    );
  }

  /** V2: mensajes de voz recibidos. */
  private consultarVoz(): Observable<Notificacion[]> {
    return this.voz.listar().pipe(
      map((lista) =>
        lista.map((m): Notificacion => ({
          id: `mensaje_voz:${m.id}`,
          tipo: 'mensaje_voz',
          titulo: `Mensaje de voz de ${m.from_name}`,
          detalle: 'Toca para escucharlo',
          fecha: m.created_at,
          ruta: '/mensajes-voz',
        })),
      ),
    );
  }

  /** R1: recordatorios pendientes de las últimas 24 horas o de la próxima hora. */
  private consultarRecordatorios(): Observable<Notificacion[]> {
    return this.recordatorios.listar().pipe(
      map((lista) => {
        const ahora = Date.now();
        return lista
          .filter((r) => {
            const hora = tiempo(r.scheduled_at);
            return r.status === 'pendiente' && hora >= ahora - DIA_MS && hora <= ahora + HORA_MS;
          })
          .map((r): Notificacion => ({
            id: `recordatorio:${r.id}`,
            tipo: 'recordatorio',
            titulo: `Recordatorio: ${r.description}`,
            detalle: diaYHora(r.scheduled_at),
            fecha: r.scheduled_at,
            ruta: '/recordatorios',
          }));
      }),
    );
  }

  /**
   * L3 y C1: llamadas del propio adulto que no fueron contestadas en las últimas 24 horas,
   * con el nombre del contacto. L3 solo lista las llamadas que hizo el adulto.
   */
  private consultarLlamadas(): Observable<Notificacion[]> {
    return forkJoin({
      llamadas: this.llamadas.llamadas(''),
      contactos: this.contactos.listar().pipe(catchError(() => of<Contacto[]>([]))),
    }).pipe(
      map(({ llamadas, contactos }) => {
        const ahora = Date.now();
        const nombres = new Map(
          contactos.map((c) => [c.id, `${c.first_name} ${c.last_name}`.trim()]),
        );
        return llamadas
          .filter((l) => l.status === 'no_contestada' && tiempo(l.started_at) >= ahora - DIA_MS)
          .map((l): Notificacion => {
            const nombre = nombres.get(l.contact_id);
            return {
              id: `llamada:${l.id}`,
              tipo: 'llamada',
              titulo: 'Tu llamada no fue contestada',
              detalle: nombre ? `A ${nombre}` : 'A un contacto',
              fecha: l.started_at,
              ruta: '/historial',
            };
          });
      }),
    );
  }

  private reiniciar(uid: string | null): void {
    this.cancelarConsulta();
    this.uid = uid;
    this.porFuente.set(SIN_NOTIFICACIONES);
    this.vistas.set(uid ? this.leerVistas(uid) : []);
  }

  private cancelarConsulta(): void {
    this.consulta?.unsubscribe();
    this.consulta = null;
  }

  private guardarVistas(nuevas: readonly string[]): void {
    const uid = this.uid;
    const actuales = this.conjuntoVistas();
    if (!uid || nuevas.every((id) => actuales.has(id))) {
      return;
    }
    const agregadas = new Set(nuevas);
    const todas = [...this.vistas().filter((id) => !agregadas.has(id)), ...agregadas].slice(
      -MAXIMO_VISTAS,
    );
    this.vistas.set(todas);
    try {
      localStorage.setItem(claveVistas(uid), JSON.stringify(todas));
    } catch {
      /* Sin almacenamiento disponible: se recuerda solo mientras la página siga abierta. */
    }
  }

  private leerVistas(uid: string): string[] {
    try {
      const texto = localStorage.getItem(claveVistas(uid));
      const datos: unknown = texto ? JSON.parse(texto) : [];
      return Array.isArray(datos) ? datos.filter((id): id is string => typeof id === 'string') : [];
    } catch {
      return [];
    }
  }
}
