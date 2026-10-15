import { Routes } from '@angular/router';

import { authGuard } from './core/auth-guard';
import { Login } from './features/auth/login';
import { PanelPrincipal } from './features/contactos/panel-principal';
import { ConexionPrueba } from './shared/conexion-prueba';

export const routes: Routes = [
  { path: 'login', component: Login },
  { path: 'inicio', component: PanelPrincipal, canActivate: [authGuard] },
  { path: 'prueba-conexion', component: ConexionPrueba },
  { path: '', pathMatch: 'full', redirectTo: 'login' },
  { path: '**', redirectTo: 'login' },
];