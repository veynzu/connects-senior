import { Injectable, Injector, computed, effect, inject, signal, untracked } from '@angular/core';
import { Observable, Subscription, catchError, forkJoin, map, of, switchMap } from 'rxjs';

import { Adultos } from '../features/auth/adultos';
import {
  Alertas,
  ETIQUETAS_CATEGORIA,
  ETIQUETAS_ESTADO,
  Solicitud,
} from '../features/ayuda/alertas';
import {
  EstadoLlamada,
  HistorialLlamadas,
  LlamadaHistorial,
} from '../features/comunicacion/historial';
import { Contacto, Contactos } from '../features/contactos/contactos';
import { MensajesVoz } from '../features/mensajes/mensajes-voz';
import {
  ETIQUETAS_ESTADO_RECORDATORIO,
  Recordatorio,
  Recordatorios,
} from '../features/recordatorios/recordatorios';
import { Auth, Rol } from './auth';

/*
 * Campana de notificaciones: reúne lo que ya está guardado en el backend (pedidos de ayuda,
 * recordatorios, mensajes de voz y llamadas). No son notificaciones push (A10): se consulta
 * cada minuto mientras haya sesión y cada vez que se abre el panel.
 *
 * Cada persona tiene un historial en este navegador (localStorage). Cada consulta añade lo
 * nuevo y actualiza lo que ya estaba (textos, fecha y estado) sin borrar nada: una llamada
 * completada o una solicitud atendida siguen en la lista. Lo que se cierra (solicitud
 * atendida, llamada completada o cancelada, recordatorio confirmado u omitido) queda como visto.
 *
 * Los mensajes de texto (M2) no se incluyen: el backend no marca mensajes leídos o no
 * leídos y revisarlos exigiría una consulta por cada contacto.
 */

export type TipoNotificacion = 'alerta' | 'recordatorio' | 'mensaje_voz' | 'llamada';

/** 'activa' = sigue pendiente; 'cerrada' = ya se resolvió y queda solo como historial. */
export type EstadoNotificacion = 'activa' | 'cerrada';

/** Lo que entrega una fuente en cada consulta. */
export interface DatosNotificacion {
  /** `tipo:id` del recurso: identifica el elemento dentro del historial. */
  id: string;
  tipo: TipoNotificacion;
  titulo: string;
  detalle: string;
  /** Fecha ISO 8601: ordena la lista y da la hora relativa. */
  fecha: string;
  ruta: string;
  adultoId?: string;
  estado: EstadoNotificacion;
}

/** Elemento del historial: lo que dio la fuente más si la persona ya lo vio. */
export interface Notificacion extends DatosNotificacion {
  vista: boolean;
}

/** null = la consulta de esa fuente falló y su parte del historial queda como estaba. */
type Resultado = DatosNotificacion[] | null;

interface SesionActiva {
  uid: string;
  rol: Rol | null;
}

interface HistorialLeido {
  lista: Notificacion[];
  /** false si esta persona todavía no tenía historial guardado (se migran sus vistas antiguas). */
  existe: boolean;
}

const MINUTO_MS = 60_000;
const HORA_MS = 60 * MINUTO_MS;
const DIA_MS = 24 * HORA_MS;
const INTERVALO_MS = MINUTO_MS;
/** Máximo de elementos que se guardan por persona (se conservan los más recientes). */
const MAXIMO_HISTORIAL = 200;
/** Se descartan los elementos con fecha anterior a 30 días. */
const RETENCION_MS = 30 * DIA_MS;

const TIPOS: readonly TipoNotificacion[] = ['alerta', 'recordatorio', 'mensaje_voz', 'llamada'];

const DETALLE_LLAMADA_CERRADA: Record<Exclude<EstadoLlamada, 'no_contestada'>, string> = {
  completada: 'Completada',
  cancelada: 'Cancelada',
};

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

function alertaANotificacion(s: Solicitud, adultoId: string): DatosNotificacion {
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
    estado: s.status === 'pendiente' || s.status === 'en_atencion' ? 'activa' : 'cerrada',
  };
}

function recordatorioANotificacion(r: Recordatorio): DatosNotificacion {
  const cuando = diaYHora(r.scheduled_at);
  const pendiente = r.status === 'pendiente';
  const etiqueta = ETIQUETAS_ESTADO_RECORDATORIO[r.status] ?? r.status;
  return {
    id: `recordatorio:${r.id}`,
    tipo: 'recordatorio',
    titulo: `Recordatorio: ${r.description}`,
    detalle: pendiente ? cuando : `${etiqueta} · ${cuando}`,
    fecha: r.scheduled_at,
    ruta: '/recordatorios',
    estado: pendiente ? 'activa' : 'cerrada',
  };
}

function llamadaANotificacion(l: LlamadaHistorial, nombre: string | undefined): DatosNotificacion {
  const comun = {
    id: `llamada:${l.id}`,
    tipo: 'llamada' as const,
    fecha: l.started_at,
    ruta: '/historial',
  };
  if (l.status === 'no_contestada') {
    return {
      ...comun,
      titulo: 'Tu llamada no fue contestada',
      detalle: nombre ? `A ${nombre}` : 'A un contacto',
      estado: 'activa',
    };
  }
  return {
    ...comun,
    titulo: nombre ? `Llamada a ${nombre}` : 'Llamada a un contacto',
    detalle: DETALLE_LLAMADA_CERRADA[l.status] ?? l.status,
    estado: 'cerrada',
  };
}

function claveHistorial(uid: string): string {
  return `cs.notificaciones.historial.${uid}`;
}

/** Clave de la versión anterior de la campana (solo ids vistos); se lee una vez para migrar. */
function claveVistasAntiguas(uid: string): string {
  return `cs.notificaciones.vistas.${uid}`;
}

/** Quita lo de más de 30 días, ordena de lo más reciente a lo más antiguo y deja 200. */
function normalizar(lista: readonly Notificacion[], ahora = Date.now()): Notificacion[] {
  const limite = ahora - RETENCION_MS;
  return lista
    .filter((n) => tiempo(n.fecha) >= limite)
    .sort((a, b) => tiempo(b.fecha) - tiempo(a.fecha))
    .slice(0, MAXIMO_HISTORIAL);
}

function esTipo(valor: unknown): valor is TipoNotificacion {
  return TIPOS.some((tipo) => tipo === valor);
}

/** Valida un elemento leído del almacenamiento; null si no tiene la forma esperada. */
function leerElemento(valor: unknown): Notificacion | null {
  if (typeof valor !== 'object' || valor === null) {
    return null;
  }
  const { id, tipo, titulo, detalle, fecha, ruta, adultoId, vista, estado } = valor as Record<
    string,
    unknown
  >;
  if (
    typeof id !== 'string' ||
    !esTipo(tipo) ||
    typeof titulo !== 'string' ||
    typeof detalle !== 'string' ||
    typeof fecha !== 'string' ||
    typeof ruta !== 'string' ||
    !ruta.startsWith('/') ||
    ruta.startsWith('//') ||
    (adultoId !== undefined && typeof adultoId !== 'string') ||
    typeof vista !== 'boolean' ||
    (estado !== 'activa' && estado !== 'cerrada')
  ) {
    return null;
  }
  return {
    id,
    tipo,
    titulo,
    detalle,
    fecha,
    ruta,
    ...(adultoId === undefined ? {} : { adultoId }),
    vista,
    estado,
  };
}

function leerHistorial(uid: string): HistorialLeido {
  try {
    const texto = localStorage.getItem(claveHistorial(uid));
    if (texto === null) {
      return { lista: [], existe: false };
    }
    const datos: unknown = JSON.parse(texto);
    const validos = Array.isArray(datos)
      ? datos.map(leerElemento).filter((n): n is Notificacion => n !== null)
      : [];
    const sinRepetidos = [...new Map(validos.map((n) => [n.id, n])).values()];
    return { lista: normalizar(sinRepetidos), existe: true };
  } catch {
    return { lista: [], existe: false };
  }
}

function leerVistasAntiguas(uid: string): Set<string> {
  try {
    const texto = localStorage.getItem(claveVistasAntiguas(uid));
    const datos: unknown = texto ? JSON.parse(texto) : [];
    return new Set(
      Array.isArray(datos) ? datos.filter((id): id is string => typeof id === 'string') : [],
    );
  } catch {
    return new Set();
  }
}

/** false si no hay almacenamiento: el historial vive solo mientras la página siga abierta. */
function escribirHistorial(uid: string, texto: string): boolean {
  try {
    localStorage.setItem(claveHistorial(uid), texto);
    return true;
  } catch {
    return false;
  }
}

function borrarVistasAntiguas(uid: string): void {
  try {
    localStorage.removeItem(claveVistasAntiguas(uid));
  } catch {
    /* Sin almacenamiento disponible: no hay nada que borrar. */
  }
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

  private readonly historial = signal<readonly Notificacion[]>([]);

  /** Todo el historial (vistas y sin ver), de lo más reciente a lo más antiguo. */
  readonly items = this.historial.asReadonly();

  private readonly noVistas = computed(
    () =>
      new Set(
        this.historial()
          .filter((n) => !n.vista)
          .map((n) => n.id),
      ),
  );

  readonly noLeidas = computed(() => this.noVistas().size);

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
  /** Ids vistos en la versión anterior de la campana; se aplican a lo que entra por primera vez. */
  private vistasAntiguas: ReadonlySet<string> = new Set();
  /** true mientras la clave antigua siga guardada y falte una consulta completa para migrarla. */
  private migracionPendiente = false;

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

  /** Pide de nuevo las fuentes que corresponden al rol y las incorpora al historial. */
  actualizar(): void {
    // No se usa estaAutenticado(): borra la sesión cuando el id_token vence. Con el token
    // vencido, el interceptor lo renueva (A3) o cierra la sesión si la renovación falla.
    if (!this.uid || !this.auth.usuario()) {
      return;
    }
    const consultas = this.consultasPara(this.auth.usuario()?.role ?? null);
    this.cancelarConsulta();
    this.consulta = forkJoin(consultas).subscribe((resultados) => this.incorporar(resultados));
  }

  marcarTodasLeidas(): void {
    this.marcarVistas(() => true);
  }

  marcarLeida(id: string): void {
    this.marcarVistas((n) => n.id === id);
  }

  esNoLeida(id: string): boolean {
    return this.noVistas().has(id);
  }

  private consultasPara(rol: Rol | null): Observable<Resultado>[] {
    const fuentes: Observable<DatosNotificacion[]>[] = [];
    if (rol === 'familiar' || rol === 'cuidador') {
      fuentes.push(this.consultarAlertas(), this.consultarVoz());
    } else if (rol === 'adulto_mayor') {
      fuentes.push(this.consultarVoz(), this.consultarRecordatorios(), this.consultarLlamadas());
    }
    // administrador (o sin rol): no hay fuentes; la campana muestra "Sin notificaciones".
    return fuentes.map((fuente) => fuente.pipe(catchError(() => of(null))));
  }

  /** A8 y luego H2 por cada adulto: todas sus solicitudes (las atendidas quedan cerradas). */
  private consultarAlertas(): Observable<DatosNotificacion[]> {
    return this.adultos.listar().pipe(
      switchMap((adultos) =>
        adultos.length === 0
          ? of<DatosNotificacion[]>([])
          : forkJoin(
              adultos.map((adulto) =>
                this.alertas.listar(adulto.id).pipe(
                  map((lista) => lista.map((s) => alertaANotificacion(s, adulto.id))),
                  // Si falla un adulto, sus elementos del historial quedan como estaban.
                  catchError(() => of<DatosNotificacion[]>([])),
                ),
              ),
            ).pipe(map((listas) => listas.flat())),
      ),
    );
  }

  /** V2: mensajes de voz recibidos (siempre activos: no tienen estado en el backend). */
  private consultarVoz(): Observable<DatosNotificacion[]> {
    return this.voz.listar().pipe(
      map((lista) =>
        lista.map((m): DatosNotificacion => ({
          id: `mensaje_voz:${m.id}`,
          tipo: 'mensaje_voz',
          titulo: `Mensaje de voz de ${m.from_name}`,
          detalle: 'Toca para escucharlo',
          fecha: m.created_at,
          ruta: '/mensajes-voz',
          estado: 'activa',
        })),
      ),
    );
  }

  /** R1: recordatorios de las últimas 24 horas o de la próxima hora, en cualquier estado. */
  private consultarRecordatorios(): Observable<DatosNotificacion[]> {
    return this.recordatorios.listar().pipe(
      map((lista) => {
        const ahora = Date.now();
        return lista
          .filter((r) => {
            const hora = tiempo(r.scheduled_at);
            return hora >= ahora - DIA_MS && hora <= ahora + HORA_MS;
          })
          .map(recordatorioANotificacion);
      }),
    );
  }

  /**
   * L3 y C1: llamadas del propio adulto de las últimas 24 horas, en cualquier estado, con el
   * nombre del contacto. L3 solo lista las llamadas que hizo el adulto; una llamada en curso
   * figura como no_contestada hasta que se cuelga (L2) y entonces pasa a cerrada.
   */
  private consultarLlamadas(): Observable<DatosNotificacion[]> {
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
          .filter((l) => tiempo(l.started_at) >= ahora - DIA_MS)
          .map((l) => llamadaANotificacion(l, nombres.get(l.contact_id)));
      }),
    );
  }

  /**
   * Upsert de lo que devolvieron las fuentes: lo que ya estaba actualiza textos, fecha, ruta y
   * estado y conserva `vista`; lo nuevo entra sin ver. Nada se borra por no venir en la
   * consulta (una fuente que falló llega como null y no aporta nada). Lo que llega o pasa a
   * 'cerrada' queda visto.
   */
  private incorporar(resultados: readonly Resultado[]): void {
    const uid = this.uid;
    if (!uid) {
      return;
    }
    const recibidos = resultados.flatMap((lista) => lista ?? []);
    const guardado = this.cambiarHistorial((actual) => {
      const porId = new Map(actual.map((n) => [n.id, n]));
      for (const datos of recibidos) {
        const anterior = porId.get(datos.id);
        const vista =
          datos.estado === 'cerrada' ||
          (anterior ? anterior.vista : this.vistasAntiguas.has(datos.id));
        porId.set(datos.id, { ...datos, vista });
      }
      return [...porId.values()];
    });
    // La clave antigua se borra cuando una consulta completa (sin fuentes fallidas) ya quedó
    // guardada en el historial; hasta entonces se sigue aplicando desde memoria.
    if (this.migracionPendiente && guardado && resultados.every((lista) => lista !== null)) {
      this.migracionPendiente = false;
      borrarVistasAntiguas(uid);
    }
  }

  private marcarVistas(debeMarcarse: (n: Notificacion) => boolean): void {
    if (!this.historial().some((n) => !n.vista && debeMarcarse(n))) {
      return;
    }
    this.cambiarHistorial((actual) =>
      actual.map((n) => (!n.vista && debeMarcarse(n) ? { ...n, vista: true } : n)),
    );
  }

  /**
   * Aplica un cambio al historial, lo deja dentro de los límites (30 días, 200 elementos) y lo
   * guarda. Respeta las marcas de vista que otra pestaña haya guardado: una notificación vista
   * nunca vuelve a "sin ver". Devuelve false si no se pudo guardar (queda solo en memoria).
   */
  private cambiarHistorial(cambio: (actual: readonly Notificacion[]) => Notificacion[]): boolean {
    const uid = this.uid;
    if (!uid) {
      return false;
    }
    const vistasGuardadas = new Set(
      leerHistorial(uid)
        .lista.filter((n) => n.vista)
        .map((n) => n.id),
    );
    const nuevo = normalizar(
      cambio(this.historial()).map((n) =>
        !n.vista && vistasGuardadas.has(n.id) ? { ...n, vista: true } : n,
      ),
    );
    const texto = JSON.stringify(nuevo);
    if (texto !== JSON.stringify(this.historial())) {
      this.historial.set(nuevo);
    }
    return escribirHistorial(uid, texto);
  }

  private reiniciar(uid: string | null): void {
    this.cancelarConsulta();
    this.uid = uid;
    const leido: HistorialLeido = uid ? leerHistorial(uid) : { lista: [], existe: true };
    this.historial.set(leido.lista);
    this.migracionPendiente = !leido.existe;
    this.vistasAntiguas = uid && !leido.existe ? leerVistasAntiguas(uid) : new Set();
  }

  private cancelarConsulta(): void {
    this.consulta?.unsubscribe();
    this.consulta = null;
  }
}
