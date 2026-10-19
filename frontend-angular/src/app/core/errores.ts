import { HttpErrorResponse } from '@angular/common/http';

/** El backend entrega los errores como { "detail": "texto" }. */
export function detalleError(error: HttpErrorResponse): string {
    const detalle = (error.error as { detail?: unknown } | null)?.detail;
    return typeof detalle === 'string' ? detalle : '';
}

/** Contrato v1.0 + backend: correo repetido responde 422 "El correo ya está registrado." */
export function correoDuplicado(error: HttpErrorResponse): boolean {
    return (
        error.status === 409 ||
        (error.status === 422 && detalleError(error).includes('correo ya está registrado'))
    );
}