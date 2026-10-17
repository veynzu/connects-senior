import { Routes } from '@angular/router';

import { authGuard } from './core/auth-guard';
import { AlertaRecibida } from './features/ayuda/alerta-recibida';
import { SolicitarAyuda } from './features/ayuda/solicitar-ayuda';
import { Solicitudes } from './features/ayuda/solicitudes';
import { PanelFamiliar } from './features/auth/panel-familiar';
import { Login } from './features/auth/login';
import { Recuperar } from './features/auth/recuperar';
import { RegistrarAdulto } from './features/auth/registrar-adulto';
import { Registro } from './features/auth/registro';
import { Llamada } from './features/comunicacion/llamada';
import { AgregarContacto } from './features/contactos/agregar-contacto';
import { ContactosAdulto } from './features/contactos/contactos-adulto';
import { DetalleContacto } from './features/contactos/detalle-contacto';
import { MisContactos } from './features/contactos/mis-contactos';
import { PanelPrincipal } from './features/contactos/panel-principal';
import { ConexionPrueba } from './shared/conexion-prueba';

export const routes: Routes = [
  { path: 'login', component: Login },
  { path: 'registro', component: Registro },
  { path: 'recuperar', component: Recuperar },
  { path: 'inicio', component: PanelPrincipal, canActivate: [authGuard] },
  { path: 'familiar', component: PanelFamiliar, canActivate: [authGuard] },
  { path: 'adultos/nuevo', component: RegistrarAdulto, canActivate: [authGuard] },
  { path: 'adultos/:adultoId/contactos', component: ContactosAdulto, canActivate: [authGuard] },
  {
    path: 'adultos/:adultoId/contactos/nuevo',
    component: AgregarContacto,
    canActivate: [authGuard],
  },
  {
    path: 'adultos/:adultoId/contactos/:id/editar',
    component: AgregarContacto,
    canActivate: [authGuard],
  },
  {
    path: 'adultos/:adultoId/contactos/:id',
    component: DetalleContacto,
    canActivate: [authGuard],
  },
  { path: 'adultos/:adultoId/solicitudes', component: Solicitudes, canActivate: [authGuard] },
  {
    path: 'adultos/:adultoId/solicitudes/:id',
    component: AlertaRecibida,
    canActivate: [authGuard],
  },
  { path: 'contactos', component: MisContactos, canActivate: [authGuard] },
  { path: 'ayuda', component: SolicitarAyuda, canActivate: [authGuard] },
  { path: 'llamada/:id', component: Llamada, canActivate: [authGuard] },
  { path: 'prueba-conexion', component: ConexionPrueba },
  { path: '', pathMatch: 'full', redirectTo: 'login' },
  { path: '**', redirectTo: 'login' },
];