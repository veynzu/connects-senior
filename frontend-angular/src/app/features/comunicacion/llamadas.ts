import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import { API_BASE_URL } from '../../core/api-config';

/* Contrato v1.0, endpoints L1 y L2. Todavía no existen en el backend. */
export type EstadoLlamada = 'completada' | 'no_contestada' | 'cancelada';

export interface LlamadaIniciada {
    call_id: string;
    room_url: string;
    token: string;
}

@Injectable({ providedIn: 'root' })
export class Llamadas {
    private readonly http = inject(HttpClient);

    /** POST /api/calls (L1). `soloAudio` verdadero = "Llamar"; falso = "Videollamada". */
    iniciar(contactId: string, soloAudio: boolean): Observable<LlamadaIniciada> {
        return this.http.post<LlamadaIniciada>(`${API_BASE_URL}/api/calls`, {
            contact_id: contactId,
            audio_only: soloAudio,
        });
    }

    /** PATCH /api/calls/{call_id} (L2). */
    actualizar(callId: string, estado: EstadoLlamada): Observable<unknown> {
        return this.http.patch<unknown>(`${API_BASE_URL}/api/calls/${callId}`, { status: estado });
    }
}