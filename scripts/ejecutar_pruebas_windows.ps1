<#
Connects Senior - Ejecucion local de pruebas automatizadas (P1, P2, P3).
No ejecuta SQL contra Neon ni envia notificaciones reales.
Requiere Python y Node 24.12+ instalados y dependencias ya instaladas.
Uso: powershell -ExecutionPolicy Bypass -File .\scripts\ejecutar_pruebas_windows.ps1
#>
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$raiz = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backend = Join-Path $raiz 'backend-fastapi'
$frontend = Join-Path $raiz 'frontend-angular'
$salida = Join-Path $raiz 'docs\pruebas\evidencias\AUTOMATIZADAS'
New-Item -ItemType Directory -Path $salida -Force | Out-Null
$marca = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$filas = [System.Collections.Generic.List[string]]::new()

function Registrar($nombre, $estado, $detalle) {
    $filas.Add("$nombre | $estado | $detalle")
    Write-Host "[$estado] $nombre - $detalle"
}

function Ejecutar($nombre, $ejecutable, $argumentos, $directorio) {
    $clave = $nombre.Replace(' ', '_')
    $stdout = Join-Path $salida "EV-${clave}-${marca}.stdout.txt"
    $stderr = Join-Path $salida "EV-${clave}-${marca}.stderr.txt"
    try {
        $proceso = Start-Process -FilePath $ejecutable -ArgumentList $argumentos `
            -WorkingDirectory $directorio -Wait -PassThru -NoNewWindow `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        $estado = if ($proceso.ExitCode -eq 0) { 'APROBADO' } else { 'NO_APROBADO' }
        Registrar $nombre $estado "codigo=$($proceso.ExitCode); salida=$([IO.Path]::GetFileName($stdout))"
    } catch {
        # No incluir en el reporte credenciales ni cadenas completas de conexion.
        Registrar $nombre 'BLOQUEADO' 'No se pudo iniciar el comando; revisar la instalacion y los archivos .stdout/.stderr'
    }
}

Write-Host '=== Connects Senior / Verificacion de calidad local ==='
Write-Host 'No se usan servicios externos ni una base Neon para estas suites.'

$py = Join-Path $backend '.venv\Scripts\python.exe'
if (Test-Path $py) {
    Ejecutar 'PYTHON_UNITARIAS_P1_P2_P3' $py @('ejecutar_pruebas_calidad.py','--suite','unitarias') $backend
    Ejecutar 'PYTHON_SINTAXIS' $py @('ejecutar_pruebas_calidad.py','--suite','sintaxis') $backend
    Ejecutar 'POSTGRES_DDL_REVISION_ESTATICA' $py @('ejecutar_pruebas_calidad.py','--suite','ddl') $backend
} else {
    Registrar 'PYTHON_UNITARIAS_P1_P2_P3' 'BLOQUEADO' 'Falta backend-fastapi\.venv. Ejecutar py -3.13 -m venv .venv y pip install -r requirements.txt -c constraints.txt'
}

$node = Get-Command node.exe -ErrorAction SilentlyContinue
$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $node -or $null -eq $npm) {
    Registrar 'ANGULAR_VITEST_P1_P2_P3' 'BLOQUEADO' 'Falta Node.js o npm en PATH'
} else {
    $versionTexto = (& $node.Source --version).Trim().TrimStart('v')
    if ([version]$versionTexto -lt [version]'24.12.0') {
        Registrar 'ANGULAR_VITEST_P1_P2_P3' 'BLOQUEADO' "Node v$versionTexto; instalar Node 24.12.0 o superior"
    } elseif (-not (Test-Path (Join-Path $frontend 'node_modules\@angular\core'))) {
        Registrar 'ANGULAR_VITEST_P1_P2_P3' 'BLOQUEADO' 'Faltan paquetes de Angular: ejecutar npm ci desde frontend-angular'
    } else {
        Ejecutar 'ANGULAR_VITEST_P1_P2_P3' $npm.Source @('run','test:ci') $frontend
        Ejecutar 'ANGULAR_BUILD_DESARROLLO' $npm.Source @('run','build','--','--configuration','development') $frontend
    }
}

$resumen = Join-Path $salida "RESUMEN_EJECUCION_${marca}.txt"
$encabezado = @(
    'Connects Senior - Ejecucion local automatizada',
    "Fecha UTC: $((Get-Date).ToUniversalTime().ToString('o'))",
    'Alcance: pruebas unitarias/mocks de P1-P2-P3, sintaxis, DDL y Angular.',
    'No prueba por si solo la integracion real con Neon/Firebase/FCM/Daily.co.',
    'NO implica que los CP/PN manuales hayan sido ejecutados.',
    ''
)
$reporte = $encabezado + $filas.ToArray()
$reporte | Set-Content -Path $resumen -Encoding UTF8
Write-Host "`nResumen guardado en: $resumen"
if (@($filas | Where-Object { $_ -match '\| (BLOQUEADO|NO_APROBADO) \|' }).Count -gt 0) {
    Write-Host 'Hay verificaciones pendientes o fallidas. Conservar evidencia y corregir la causa.'
    exit 1
}
Write-Host 'Las verificaciones disponibles finalizaron correctamente.'
exit 0
