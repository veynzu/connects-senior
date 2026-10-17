import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';

/*
 * Contrato v1.0, sección 5 (H1 a H5). Estos endpoints todavía NO existen en el backend.
 */
export type CategoriaAyuda = 'ayuda_en_casa' | 'compania' | 'problema' | 'otro';
export type EstadoSolicitud = 'pendiente' | 'en_atencion' | 'atendida';

export const ETIQUETAS_CATEGORIA: Record<CategoriaAyuda, string> = {
    ayuda_en_casa: 'Necesita ayuda en casa',
    compania: 'Necesita compañía',
    problema: 'Tiene un problema',
    otro: 'Otro',
};

export const ETIQUETAS_ESTADO: Record<EstadoSolicitud, string> = {
    pendiente: 'Pendiente',
    en_atencion: 'En atención',
    atendida: 'Atendida',
};

export interface NuevaSolicitud {
    category?: CategoriaAyuda;
    latitude?: number;
    longitude?: number;
}

/** Respuesta de H1 al crear la solicitud (lo que ve el adulto mayor). */
export interface SolicitudAyuda {
    id: string;
    status: string;
    created_at: string;
    location_available: boolean;
    has_trusted_contacts: boolean;
}

/** Elemento de la lista H2 (lo que ve el familiar o cuidador). */
export interface Solicitud {
    id: string;
    adult_name: string;
    category: CategoriaAyuda | null;
    status: EstadoSolicitud;
    created_at: string;
    location_available: boolean;
}

/** H3: incluye las coordenadas solo si el adulto mayor las compartió. */
export interface SolicitudDetalle extends Solicitud {
    latitude?: number | null;
    longitude?: number | null;
}

export function fechaLegible(iso: string): string {
    return new Date(iso).toLocaleDateString('es-CO');
}

export function horaLegible(iso: string): string {
    return new Date(iso).toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
}

@Injectable({ providedIn: 'root' })
export class Alertas {
    private readonly http = inject(HttpClient);

    /** POST /api/alerts (H1). El adulto mayor se deduce del token. */
    crear(datos: NuevaSolicitud): Observable<SolicitudAyuda> {
        return this.http.post<SolicitudAyuda>(`${API_BASE_URL}/api/alerts`, datos);
    }

    /** GET /api/alerts?adult_id= (H2) */
    listar(adultId: string): Observable<Solicitud[]> {
        const params = new HttpParams().set('adult_id', adultId);
        return this.http.get<Solicitud[]>(`${API_BASE_URL}/api/alerts`, { params });
    }

    /** GET /api/alerts/{id} (H3) */
    obtener(id: string): Observable<SolicitudDetalle> {
        return this.http.get<SolicitudDetalle>(`${API_BASE_URL}/api/alerts/${id}`);
    }

    /** POST /api/alerts/{id}/acknowledge (H4): pasa la solicitud a "en atención". */
    atender(id: string): Observable<SolicitudDetalle> {
        return this.http.post<SolicitudDetalle>(`${API_BASE_URL}/api/alerts/${id}/acknowledge`, {});
    }

    /** PATCH /api/alerts/{id} (H5) */
    cambiarEstado(id: string, estado: EstadoSolicitud): Observable<SolicitudDetalle> {
        return this.http.patch<SolicitudDetalle>(`${API_BASE_URL}/api/alerts/${id}`, {
            status: estado,
        });
    }
}