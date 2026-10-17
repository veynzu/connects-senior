import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';

/*
 * Contrato v1.0, sección 6 (R1 a R6). Estos endpoints todavía NO existen en el backend.
 */
export type TipoRecordatorio = 'cita_medica' | 'medicamento';
export type Repeticion = 'ninguna' | 'diaria' | 'semanal';
export type EstadoRecordatorio = 'pendiente' | 'confirmado' | 'omitido';

export const ETIQUETAS_TIPO: Record<TipoRecordatorio, string> = {
    cita_medica: 'Cita médica',
    medicamento: 'Medicamento',
};

export const ETIQUETAS_REPETICION: Record<Repeticion, string> = {
    ninguna: 'No se repite',
    diaria: 'Todos los días',
    semanal: 'Todas las semanas',
};

export const ETIQUETAS_ESTADO_RECORDATORIO: Record<EstadoRecordatorio, string> = {
    pendiente: 'Pendiente',
    confirmado: 'Confirmado',
    omitido: 'Omitido',
};

export interface Recordatorio {
    id: string;
    kind: TipoRecordatorio;
    description: string;
    scheduled_at: string;
    repeat: Repeticion;
    status: EstadoRecordatorio;
}

export interface DatosRecordatorio {
    kind: TipoRecordatorio;
    description: string;
    scheduled_at: string;
    repeat: Repeticion;
}

export function fechaHoraLegible(iso: string): string {
    const f = new Date(iso);
    return `${f.toLocaleDateString('es-CO')} · ${f.toLocaleTimeString('es-CO', {
        hour: '2-digit',
        minute: '2-digit',
    })}`;
}

@Injectable({ providedIn: 'root' })
export class Recordatorios {
    private readonly http = inject(HttpClient);
    private readonly url = `${API_BASE_URL}/api/reminders`;

    /** GET /api/reminders (R1). adult_id solo lo envía el familiar o cuidador. */
    listar(adultId?: string): Observable<Recordatorio[]> {
        let params = new HttpParams();
        if (adultId) {
            params = params.set('adult_id', adultId);
        }
        return this.http.get<Recordatorio[]>(this.url, { params });
    }

    /** POST /api/reminders (R2) */
    crear(datos: DatosRecordatorio, adultId: string): Observable<Recordatorio> {
        return this.http.post<Recordatorio>(this.url, { adult_id: adultId, ...datos });
    }

    /** PATCH /api/reminders/{id} (R3) */
    actualizar(id: string, datos: DatosRecordatorio): Observable<Recordatorio> {
        return this.http.patch<Recordatorio>(`${this.url}/${id}`, datos);
    }

    /** DELETE /api/reminders/{id} (R4): responde 204 */
    eliminar(id: string): Observable<void> {
        return this.http.delete<void>(`${this.url}/${id}`);
    }

    /** POST /api/reminders/{id}/confirm (R5) */
    confirmar(id: string): Observable<Recordatorio> {
        return this.http.post<Recordatorio>(`${this.url}/${id}/confirm`, {});
    }

    /** POST /api/reminders/{id}/snooze (R6): el frontend envía 10 minutos. */
    posponer(id: string): Observable<Recordatorio> {
        return this.http.post<Recordatorio>(`${this.url}/${id}/snooze`, { minutes: 10 });
    }
}