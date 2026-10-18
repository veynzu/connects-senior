import { Routes } from '@angular/router';

import { authGuard } from './core/auth-guard';
import { AlertaRecibida } from './features/ayuda/alerta-recibida';
import { SolicitarAyuda } from './features/ayuda/solicitar-ayuda';
import { Solicitudes } from './features/ayuda/solicitudes';
import { InvitarCuidador } from './features/auth/invitar-cuidador';
import { PanelFamiliar } from './features/auth/panel-familiar';
import { Perfil } from './features/auth/perfil';
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
import { Conversacion } from './features/mensajes/conversacion';
import { ListaMensajes } from './features/mensajes/lista-mensajes';
import { FormularioRecordatorio } from './features/recordatorios/formulario-recordatorio';
import { MisRecordatorios } from './features/recordatorios/mis-recordatorios';
import { RecordatoriosAdulto } from './features/recordatorios/recordatorios-adulto';
import { ConexionPrueba } from './shared/conexion-prueba';

export const routes: Routes = [
  { path: 'login', component: Login },
  { path: 'registro', component: Registro },
  { path: 'recuperar', component: Recuperar },
  { path: 'inicio', component: PanelPrincipal, canActivate: [authGuard] },
  { path: 'familiar', component: PanelFamiliar, canActivate: [authGuard] },
  { path: 'perfil', component: Perfil, canActivate: [authGuard] },
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
  {
    path: 'adultos/:adultoId/recordatorios',
    component: RecordatoriosAdulto,
    canActivate: [authGuard],
  },
  {
    path: 'adultos/:adultoId/recordatorios/nuevo',
    component: FormularioRecordatorio,
    canActivate: [authGuard],
  },
  {
    path: 'adultos/:adultoId/recordatorios/:id/editar',
    component: FormularioRecordatorio,
    canActivate: [authGuard],
  },
  {
    path: 'adultos/:adultoId/cuidadores/nuevo',
    component: InvitarCuidador,
    canActivate: [authGuard],
  },
  { path: 'recordatorios', component: MisRecordatorios, canActivate: [authGuard] },
  { path: 'mensajes', component: ListaMensajes, canActivate: [authGuard] },
  { path: 'mensajes/:contactoId', component: Conversacion, canActivate: [authGuard] },
  { path: 'contactos', component: MisContactos, canActivate: [authGuard] },
  { path: 'ayuda', component: SolicitarAyuda, canActivate: [authGuard] },
  { path: 'llamada/:id', component: Llamada, canActivate: [authGuard] },
  { path: 'prueba-conexion', component: ConexionPrueba },
  { path: '', pathMatch: 'full', redirectTo: 'login' },
  { path: '**', redirectTo: 'login' },
];