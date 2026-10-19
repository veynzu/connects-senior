/**
 * Configuración de desarrollo (ng serve, ng build --configuration development): el backend local.
 * angular.json la usa en lugar de environment.ts mediante fileReplacements.
 */
export const environment = {
  production: false,
  apiBaseUrl: 'http://127.0.0.1:8000',
  /**
   * Configuración web pública de Firebase (proyecto connect-senior). Firebase la publica por
   * diseño: solo identifica la app; no es un secreto ni da acceso de administrador.
   */
  firebase: {
    apiKey: 'AIzaSyCLBGsps_Q4pTOHzJRt7sflyPaNVlwGTaY',
    authDomain: 'connect-senior.firebaseapp.com',
    projectId: 'connect-senior',
    storageBucket: 'connect-senior.firebasestorage.app',
    messagingSenderId: '449020125238',
    appId: '1:449020125238:web:5d7aa1c5376e355fcdb897',
  },
  /** Clave VAPID de Cloud Messaging. Vacía = la clave por defecto del SDK de Firebase. */
  fcmVapidKey: '',
};
