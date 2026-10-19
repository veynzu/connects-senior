import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';

/*
 * Contrato v1.0, sección 3 (C1 a C4). Estos endpoints todavía NO existen en el backend.
 * Campos tomados de los mockups UI-4, UI-5 y UI-6.
 */
export interface PermisosContacto {
    calls: boolean;
    video_calls: boolean;
    messages: boolean;
    help_alerts: boolean;
}

export interface Contacto {
    id: string;
    first_name: string;
    last_name: string;
    relationship: string;
    phone: string | null;
    email: string | null;
    photo_url: string | null;
    is_favorite: boolean;
    favorite_order: number | null;
    permissions: PermisosContacto;
}

export interface DatosContacto {
    first_name: string;
    last_name: string;
    relationship: string;
    phone: string | null;
    email: string | null;
    permissions: PermisosContacto;
}

export interface NuevoContacto extends DatosContacto {
    adult_id: string;
}

@Injectable({ providedIn: 'root' })
export class Contactos {
    private readonly http = inject(HttpClient);

    /**
     * GET /api/contacts (C1). El adulto mayor no envía `adultId` (se deduce del token);
     * el familiar o cuidador sí. El backend entrega los contactos ya ordenados.
     */
    listar(adultId?: string): Observable<Contacto[]> {
        const params = adultId ? new HttpParams().set('adult_id', adultId) : undefined;
        return this.http.get<Contacto[]>(`${API_BASE_URL}/api/contacts`, { params });
    }

    /** POST /api/contacts (C2) */
    crear(datos: NuevoContacto): Observable<Contacto> {
        return this.http.post<Contacto>(`${API_BASE_URL}/api/contacts`, datos);
    }

    /** PATCH /api/contacts/{id} (C3) */
    actualizar(id: string, datos: Partial<DatosContacto>): Observable<Contacto> {
        return this.http.patch<Contacto>(`${API_BASE_URL}/api/contacts/${id}`, datos);
    }

    /** DELETE /api/contacts/{id} (C4) */
    eliminar(id: string): Observable<void> {
        return this.http.delete<void>(`${API_BASE_URL}/api/contacts/${id}`);
    }
}