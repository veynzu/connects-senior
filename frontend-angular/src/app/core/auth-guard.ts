import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';

import { Auth } from './auth';

/** Solo deja entrar a la ruta si hay una sesión vigente; si no, envía al login. */
export const authGuard: CanActivateFn = () => {
    const auth = inject(Auth);
    const router = inject(Router);
    return auth.estaAutenticado() ? true : router.createUrlTree(['/login']);
};