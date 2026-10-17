import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';

/*
 * Contrato v1.0, sección 4 (M1 y M2) y sección 3 (contactos, solo lectura).
 * Estos endpoints todavía NO existen en el backend.
 */
export interface ContactoMensaje {
    id: string;
    first_name: string;
    last_name: string;
    relationship: string;
    permissions: { messages: boolean };
}

export interface Mensaje {
    id: string;
    from_me: boolean;
    text: string;
    created_at: string;
}

export interface MensajeEnviado {
    id: string;
    created_at: string;
}

export function horaMensaje(iso: string): string {
    const f = new Date(iso);
    return `${f.toLocaleDateString('es-CO')} ${f.toLocaleTimeString('es-CO', {
        hour: '2-digit',
        minute: '2-digit',
    })}`;
}

@Injectable({ providedIn: 'root' })
export class Mensajes {
    private readonly http = inject(HttpClient);

    /** GET /api/contacts: se usa para listar con quién se puede conversar. */
    contactos(): Observable<ContactoMensaje[]> {
        return this.http.get<ContactoMensaje[]>(`${API_BASE_URL}/api/contacts`);
    }

    /** GET /api/messages?contact_id= (M2) */
    conversacion(contactId: string): Observable<Mensaje[]> {
        const params = new HttpParams().set('contact_id', contactId);
        return this.http.get<Mensaje[]>(`${API_BASE_URL}/api/messages`, { params });
    }

    /** POST /api/messages (M1) */
    enviar(contactId: string, texto: string): Observable<MensajeEnviado> {
        return this.http.post<MensajeEnviado>(`${API_BASE_URL}/api/messages`, {
            contact_id: contactId,
            text: texto,
        });
    }
}