import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';

/* Contrato v1.0, A7 y A8. Todavía no existen en el backend. */
export interface Adulto {
    id: string;
    first_name: string;
    last_name: string;
    email: string;
    phone: string | null;
    birth_date: string | null;
    address: string | null;
    photo_url: string | null;
}

export interface NuevoAdulto {
    first_name: string;
    last_name: string;
    email: string;
    password: string;
    phone?: string;
    birth_date?: string;
    address?: string;
}

@Injectable({ providedIn: 'root' })
export class Adultos {
    private readonly http = inject(HttpClient);

    /** GET /api/auth/adults (A8) */
    listar(): Observable<Adulto[]> {
        return this.http.get<Adulto[]>(`${API_BASE_URL}/api/auth/adults`);
    }

    /** POST /api/auth/adults (A7) */
    crear(datos: NuevoAdulto): Observable<Adulto> {
        return this.http.post<Adulto>(`${API_BASE_URL}/api/auth/adults`, datos);
    }
}