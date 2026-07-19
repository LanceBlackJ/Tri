$containerName = "teacher-redis-1"

$existing = docker ps -a --filter "name=$containerName" --format "{{.Names}}"
if ($existing -eq $containerName) {
    docker start $containerName | Out-Null
    Write-Host "Redis container started: $containerName"
    exit 0
}

docker run -d --name $containerName -p 6379:6379 redis:7-alpine | Out-Null
Write-Host "Redis container created and started: $containerName"