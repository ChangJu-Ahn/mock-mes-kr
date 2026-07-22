// Mock MES on Azure Container Apps (Consumption, cheapest).
//
// ONE Container App, ONE replica, scale-to-zero. All containers share an
// ephemeral EmptyDir volume mounted at /data holding the SQLite DB:
//   init container 'seed' -> writes /data/mes.db before app containers start
//   'api'  container       -> FastAPI web console + REST on :8000
//   'mcp'  container       -> MCP streamable-HTTP on :8001 (/mcp)
//   'proxy' container      -> Caddy, the single external ingress on :8080
//
// Sizing: each container 0.25 vCPU / 0.5 GiB (ACA minimum). The 3 app
// containers sum to 0.75 vCPU / 1.5 GiB per replica. minReplicas=0 => ~$0 idle.

@description('Deployment region.')
param location string = resourceGroup().location

@description('Container App name (also used as the ingress subdomain prefix).')
param appName string = 'mock-mes'

@description('App image (init + api + mcp all use this).')
param appImage string = 'ghcr.io/changju-ahn/mock-mes-app:latest'

@description('Caddy proxy image.')
param proxyImage string = 'ghcr.io/changju-ahn/mock-mes-proxy:latest'

@description('Revision suffix. Defaults to a deploy-time timestamp so each redeploy rolls a fresh revision that re-pulls the (mutable :latest) images.')
param revisionSuffix string = 'r${utcNow('yyMMddHHmmss')}'

@description('Demo API key required in the X-API-Key header on all REST /api/* and MCP /mcp calls. Web console + /api/docs stay open.')
param apiKey string = 'changjuahn'

var dbPath = '/data/mes.db'
var dbEnv = [
  {
    name: 'MES_DB_PATH'
    value: dbPath
  }
]
// api + mcp additionally get the demo API key; the seed init container does not need it.
var appEnv = concat(dbEnv, [
  {
    name: 'MES_API_KEY'
    value: apiKey
  }
])
var containerResources = {
  cpu: json('0.25')
  memory: '0.5Gi'
}
var volumeMounts = [
  {
    volumeName: 'mesdata'
    mountPath: '/data'
  }
]

resource law 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${appName}-logs'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${appName}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
    }
    template: {
      revisionSuffix: revisionSuffix
      volumes: [
        {
          name: 'mesdata'
          storageType: 'EmptyDir'
        }
      ]
      initContainers: [
        {
          name: 'seed'
          image: appImage
          command: [
            'python'
            '-m'
            'mes_core.seed'
          ]
          resources: containerResources
          env: dbEnv
          volumeMounts: volumeMounts
        }
      ]
      containers: [
        {
          name: 'api'
          image: appImage
          command: [
            'uvicorn'
            'api.main:app'
            '--host'
            '0.0.0.0'
            '--port'
            '8000'
          ]
          resources: containerResources
          env: appEnv
          volumeMounts: volumeMounts
        }
        {
          name: 'mcp'
          image: appImage
          command: [
            'python'
            '-m'
            'mcp_server'
          ]
          resources: containerResources
          env: appEnv
          volumeMounts: volumeMounts
        }
        {
          name: 'proxy'
          image: proxyImage
          resources: containerResources
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
}

output fqdn string = app.properties.configuration.ingress.fqdn
output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}/'
output restUrl string = 'https://${app.properties.configuration.ingress.fqdn}/api'
output restDocsUrl string = 'https://${app.properties.configuration.ingress.fqdn}/api/docs'
output mcpUrl string = 'https://${app.properties.configuration.ingress.fqdn}/mcp'
