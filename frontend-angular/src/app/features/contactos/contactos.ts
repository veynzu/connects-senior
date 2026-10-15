import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';

/*
 * PROPUESTA: contrato C1 del documento "Contrato Frontend–Backend".
 * Este endpoint todavía NO existe en el backend; Andrés debe confirmarlo.
 * Campos tomados de los mockups UI-4, UI-5 y UI-6.
 */
export interface PermisosContacto {
    calls: boolean;
    video_calls: boolean;
    messages: boolean;
    help_alerts: boolean;
    emergency_alerts: boolean;
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

@Injectable({ providedIn: 'root' })
export class Contactos {
    private readonly http = inject(HttpClient);

    /** GET /api/contacts (PROPUESTA). El backend entrega los contactos ya ordenados. */
    listar(): Observable<Contacto[]> {
        return this.http.get<Contacto[]>(`${API_BASE_URL}/api/contacts`);
    }
}
