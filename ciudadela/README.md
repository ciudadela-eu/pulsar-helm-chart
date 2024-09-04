# Upgrade to 3.0.6 (chart version 3.5.0)

1. Delete namespace `kubectl delete namespace pulsar`
2. Create namespace `kubectl create namespace pulsar`
3. Prepare helm release `./scripts/pulsar/prepare_helm_release.sh -n pulsar -k pulsar`
4. Install chart `helm install pulsar apache/pulsar -n pulsar -f ./charts/pulsar/values.yaml --version '3.5.0'`
5. Edit **pulsar-broker** statefulset replacing **args** field of init container **wait-zookeeper-ready** for:
   ```yaml
   - args:
       - |-
          export PULSAR_MEM="-Xmx128M"; until timeout 15 bin/pulsar zookeeper-shell -server pulsar-zookeeper get /admin/clusters/pulsar; do
           sleep 3;
         done;
   ```
6. Replace values and apply connectors configmaps `kubectl apply -f ./ciudadela/configmaps.yaml`
7. (In pulsar-broker pod shell) Create pulsar namespaces required by connectors `bin/pulsar-admin namespaces create public/[strapi, accounting]`
8. Deploy connectors with github actions
9. Setup **Pulsar Manager**
   - In pulsar-manager pod shell run:
     ```bash
     CSRF_TOKEN=$(curl http://localhost:7750/pulsar-manager/csrf-token)
     curl \
       -H 'X-XSRF-TOKEN: $CSRF_TOKEN' \
       -H 'Cookie: XSRF-TOKEN=$CSRF_TOKEN;' \
       -H "Content-Type: application/json" \
       -X PUT http://localhost:7750/pulsar-manager/users/superuser \
       -d '{"name": "admin", "password": "pulsar", "description": "main", "email": "pulsar@pulsar.com"}'
     ```
   - Port forward pulsar-manager pod 9527 port, login and create the environment (if it does not already exists):
     - Environment name: ciudadela
     - Service URL: `http://pulsar-broker:8080`
     - Bookie URL: `http://pulsar-bookie:8000`
