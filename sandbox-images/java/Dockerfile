FROM maven:3.9-eclipse-temurin-21

ENV MAVEN_OPTS="-Dmaven.repo.local=/app/.m2"

WORKDIR /app

COPY sandbox-images/proto/ ./proto/

# Copy pom and place proto where the plugin expects it
COPY sandbox-images/java/harness/pom.xml ./harness/pom.xml
RUN mkdir -p /app/harness/src/main/proto && \
    cp /app/proto/sim_interface.proto /app/harness/src/main/proto/

# Warm the Maven cache before copying source (layer cached on pom.xml changes only).
# Stub compilation triggers plugin + dep downloads; failures are expected/ignored.
RUN mkdir -p /app/harness/src/main/java && \
    printf 'public class Main { public static void main(String[] a) {} }\n' \
        > /app/harness/src/main/java/Main.java && \
    cd /app/harness && mvn package -DskipTests -q 2>/dev/null; rm -rf /app/harness/target

COPY sandbox-images/java/harness/src/ ./harness/src/

# Full build: generate proto sources, compile everything, copy deps to target/dependency/
RUN cd /app/harness && mvn package -DskipTests -q

RUN chown -R ubuntu:ubuntu /app

USER ubuntu

EXPOSE 50051
ENTRYPOINT ["java", "-Dio.grpc.netty.shaded.io.netty.transport.noNative=true", "-cp", "/app/harness/target/classes:/app/harness/target/dependency/*", "Main"]
