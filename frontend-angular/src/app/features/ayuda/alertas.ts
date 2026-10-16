import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';

/*
 * PROPUESTA: contrato H1 del documento "Contrato Frontend–Backend".
 * Este endpoint todavía NO existe en el backend; Andrés debe confirmarlo.
 */
export type CategoriaAyuda = 'ayuda_en_casa' | 'compania' | 'problema' | 'otro';

export interface NuevaSolicitud {
    category?: CategoriaAyuda;
    latitude?: number;
    longitude?: number;
}

export interface SolicitudAyuda {
    id: string;
    status: string;
    created_at: string;
    location_available: boolean;
    has_trusted_contacts: boolean;
}

@Injectable({ providedIn: 'root' })
export class Alertas {
    private readonly http = inject(HttpClient);

    /** POST /api/alerts (PROPUESTA). El adulto mayor se deduce del token. */
    crear(datos: NuevaSolicitud): Observable<SolicitudAyuda> {
        return this.http.post<SolicitudAyuda>(`${API_BASE_URL}/api/alerts`, datos);
    }
}