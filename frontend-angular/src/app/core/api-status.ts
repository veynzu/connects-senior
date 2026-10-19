import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

/** Respuesta de GET / en backend-fastapi/app/main.py */
export interface RespuestaRaiz {
    message: string;
}

@Injectable({ providedIn: 'root' })
export class ApiStatus {
    private readonly http = inject(HttpClient);

    /** GET / del backend (a través del proxy de desarrollo: /backend/) */
    consultar(): Observable<RespuestaRaiz> {
        return this.http.get<RespuestaRaiz>('/backend/');
    }
}