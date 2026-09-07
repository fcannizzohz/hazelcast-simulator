/*
 * Copyright (c) 2008-2026, Hazelcast, Inc. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package com.hazelcast.simulator.tests.map;

import com.hazelcast.core.OperationTimeoutException;
import com.hazelcast.core.Pipelining;
import com.hazelcast.map.IMap;
import com.hazelcast.simulator.probes.LatencyProbe;
import com.hazelcast.simulator.test.annotations.StartNanos;
import com.hazelcast.simulator.test.annotations.TimeStep;
import com.hazelcast.simulator.test.annotations.Verify;
import com.hazelcast.splitbrainprotection.SplitBrainProtectionException;
import com.hazelcast.spi.exception.TargetDisconnectedException;

import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.atomic.LongAdder;
import java.util.function.Consumer;
import java.util.function.Supplier;

/**
 * Retries idempotent map operations across expected member-loss and
 * split-brain-protection failures.
 *
 * <p>Only {@link SplitBrainProtectionException},
 * {@link TargetDisconnectedException}, and {@link OperationTimeoutException}
 * are retried. Every retry uses the same
 * map, key and value as the original invocation. Other failures remain fatal.
 * The test verifies that operations recovered after the final expected fault
 * and that the prepared key set was preserved.</p>
 */
public class FailureTolerantLongByteArrayMap extends LongByteArrayMapTest {

    private static final long RETRY_PAUSE_MILLIS = 100;
    private static final long MINIMUM_RECOVERY_MILLIS = 30_000;

    private final LongAdder successfulOperations = new LongAdder();
    private final LongAdder protectionFailures = new LongAdder();
    private final LongAdder targetDisconnections = new LongAdder();
    private final LongAdder operationTimeouts = new LongAdder();
    private final LongAdder pendingRetries = new LongAdder();
    private final AtomicLong lastSuccessNanos = new AtomicLong(-1);
    private final AtomicLong lastExpectedFaultNanos = new AtomicLong(-1);
    private final AtomicReference<Throwable> unexpectedAsyncFailure = new AtomicReference<>();
    private final AtomicBoolean firstExpectedFault = new AtomicBoolean();

    @Override
    @TimeStep(prob = -1)
    public byte[] get(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.fixedKeyOrRandom();
        return execute(() -> map.get(key), value -> verifyValue(value, "get"));
    }

    @Override
    @TimeStep(prob = 0)
    public Map<Long, byte[]> getAll(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        Set<Long> keys = new HashSet<>();
        for (int k = 0; k < getAllSize; k++) {
            keys.add(state.randomKey());
        }
        return execute(() -> map.getAll(keys), result -> verifyGetAll(result, keys.size()));
    }

    @Override
    @TimeStep(prob = 0)
    public CompletableFuture<byte[]> getAsync(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        return executeAsync(() -> map.getAsync(key).toCompletableFuture(), value -> verifyValue(value, "getAsync"));
    }

    @Override
    @TimeStep(prob = 0.1)
    public byte[] put(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return execute(() -> map.put(key, value), previous -> verifyValue(previous, "put"));
    }

    @Override
    @TimeStep(prob = 0)
    public void deleteAndPut(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        execute(() -> {
            map.delete(key);
            map.put(key, value);
            return null;
        }, ignored -> { });
    }

    @Override
    @TimeStep(prob = 0.0)
    public CompletableFuture<byte[]> putAsync(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return executeAsync(() -> map.putAsync(key, value).toCompletableFuture(),
                previous -> verifyValue(previous, "putAsync"));
    }

    @Override
    @TimeStep(prob = 0)
    public void set(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        execute(() -> {
            map.set(key, value);
            return null;
        }, ignored -> { });
    }

    @Override
    @TimeStep(prob = 0)
    public CompletableFuture<Void> setAsync(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return executeAsync(() -> map.setAsync(key, value).toCompletableFuture(), ignored -> { });
    }

    @Override
    @TimeStep(prob = 0)
    public void sizeLog() {
        IMap<Long, byte[]> map = getRandomMap();
        int size = execute(map::size, ignored -> { });
        logger.info("current size of {}: {}", map.getName(), size);
    }

    @Override
    @TimeStep(prob = 0)
    public void updateAllUsingEntryProcessor() {
        IMap<Long, byte[]> map = getRandomMap();
        execute(() -> map.executeOnEntries(new UpdateEntryProcessor((byte) 1)), ignored -> { });
    }

    @Override
    @TimeStep(prob = 0)
    public void pipelinedGet(ThreadState state, @StartNanos long startNanos, LatencyProbe probe) throws Exception {
        if (state.pipeline == null) {
            state.pipeline = new Pipelining<>(pipelineDepth);
        }

        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        CompletableFuture<byte[]> future = executeAsync(
                () -> map.getAsync(key).toCompletableFuture(), value -> verifyValue(value, "pipelinedGet"));
        future.whenCompleteAsync((bytes, throwable) -> probe.done(startNanos), Runnable::run);
        state.pipeline.add(future);
        state.i++;
        if (state.i == pipelineIterations) {
            state.i = 0;
            state.pipeline.results();
            state.pipeline = null;
        }
    }

    @Verify(global = false)
    public void verifyFailureRecovery() {
        Throwable asyncFailure = unexpectedAsyncFailure.get();
        if (asyncFailure != null) {
            throw new AssertionError("An asynchronous map operation failed unexpectedly", asyncFailure);
        }

        long successful = successfulOperations.sum();
        long protectedFailures = protectionFailures.sum();
        long disconnected = targetDisconnections.sum();
        long timeouts = operationTimeouts.sum();
        long expectedFaults = protectedFailures + disconnected + timeouts;
        long pending = pendingRetries.sum();
        if (pending != 0) {
            throw new AssertionError(pending + " idempotent map operations were still pending at verification");
        }
        if (successful == 0) {
            throw new AssertionError("No map operation completed successfully");
        }

        if (expectedFaults > 0) {
            long finalFaultNanos = lastExpectedFaultNanos.get();
            if (lastSuccessNanos.get() <= finalFaultNanos) {
                throw new AssertionError("No map operation succeeded after the final expected fault");
            }
            long recoveryMillis = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - finalFaultNanos);
            if (recoveryMillis < minimumRecoveryMillis()) {
                throw new AssertionError("The final expected fault occurred only " + recoveryMillis
                        + "ms before verification; expected at least " + minimumRecoveryMillis() + "ms");
            }
        }

        for (int mapIndex = 0; mapIndex < mapCount; mapIndex++) {
            String mapName = mapCount == 1 ? name : name + "_" + mapIndex;
            int actualSize = targetInstance.getMap(mapName).size();
            if (actualSize != keyDomain) {
                throw new AssertionError("Map [" + mapName + "] contains " + actualSize
                        + " entries; expected " + keyDomain);
            }
        }

        logger.info("Failure-tolerant verification passed: successfulOperations={}, protectionFailures={}, "
                        + "targetDisconnections={}, operationTimeouts={}",
                successful, protectedFailures, disconnected, timeouts);
    }

    private <T> T execute(Supplier<T> operation, Consumer<T> verifier) {
        try {
            T result = operation.get();
            verifier.accept(result);
            recordSuccess();
            return result;
        } catch (RuntimeException exception) {
            RuntimeException expectedFault = findExpectedFault(exception);
            if (expectedFault == null) {
                throw exception;
            }
            recordExpectedFault(expectedFault);
            pendingRetries.increment();
            scheduleRetry(operation, verifier);
            // Do not hold the timestep thread on a partition that is
            // temporarily protected. The exact idempotent operation is
            // retried independently while this worker continues issuing work.
            return null;
        }
    }

    private <T> void scheduleRetry(Supplier<T> operation, Consumer<T> verifier) {
        CompletableFuture.delayedExecutor(retryPauseMillis(), TimeUnit.MILLISECONDS)
                .execute(() -> retry(operation, verifier));
    }

    private <T> void retry(Supplier<T> operation, Consumer<T> verifier) {
        if (isStopped()) {
            // Keep the retry outstanding so verification can report that an
            // operation was abandoned before it became successful.
            return;
        }
        try {
            T result = operation.get();
            verifier.accept(result);
            recordSuccess();
            pendingRetries.decrement();
        } catch (RuntimeException exception) {
            RuntimeException expectedFault = findExpectedFault(exception);
            if (expectedFault == null) {
                unexpectedAsyncFailure.compareAndSet(null, exception);
                pendingRetries.decrement();
                return;
            }
            recordExpectedFault(expectedFault);
            scheduleRetry(operation, verifier);
        }
    }

    private <T> CompletableFuture<T> executeAsync(Supplier<CompletableFuture<T>> operation, Consumer<T> verifier) {
        CompletableFuture<T> result = new CompletableFuture<>();
        attemptAsync(operation, verifier, result);
        return result;
    }

    private <T> void attemptAsync(Supplier<CompletableFuture<T>> operation, Consumer<T> verifier,
                                  CompletableFuture<T> result) {
        if (isStopped()) {
            result.complete(null);
            return;
        }

        try {
            operation.get().whenComplete((value, throwable) -> {
                if (throwable == null) {
                    try {
                        verifier.accept(value);
                        recordSuccess();
                        result.complete(value);
                    } catch (Throwable failure) {
                        recordUnexpectedAsyncFailure(failure, result);
                    }
                    return;
                }

                RuntimeException expectedFault = findExpectedFault(throwable);
                if (expectedFault == null) {
                    recordUnexpectedAsyncFailure(unwrap(throwable), result);
                    return;
                }
                recordExpectedFault(expectedFault);
                CompletableFuture.delayedExecutor(retryPauseMillis(), TimeUnit.MILLISECONDS)
                        .execute(() -> attemptAsync(operation, verifier, result));
            });
        } catch (RuntimeException exception) {
            RuntimeException expectedFault = findExpectedFault(exception);
            if (expectedFault == null) {
                recordUnexpectedAsyncFailure(exception, result);
                return;
            }
            recordExpectedFault(expectedFault);
            CompletableFuture.delayedExecutor(retryPauseMillis(), TimeUnit.MILLISECONDS)
                    .execute(() -> attemptAsync(operation, verifier, result));
        }
    }

    private void recordUnexpectedAsyncFailure(Throwable failure, CompletableFuture<?> result) {
        unexpectedAsyncFailure.compareAndSet(null, failure);
        result.completeExceptionally(failure);
    }

    private void verifyGetAll(Map<Long, byte[]> result, int expectedSize) {
        if (result.size() != expectedSize) {
            throw new AssertionError("getAll returned " + result.size() + " entries; expected " + expectedSize);
        }
        for (byte[] value : result.values()) {
            verifyValue(value, "getAll");
        }
    }

    private void verifyValue(byte[] value, String operation) {
        if (value == null) {
            throw new AssertionError(operation + " returned a missing value for a prepared key");
        }
        if (value.length < minValueLength || value.length > maxValueLength) {
            throw new AssertionError(operation + " returned a value of " + value.length
                    + " bytes; expected between " + minValueLength + " and " + maxValueLength);
        }
    }

    private void recordSuccess() {
        successfulOperations.increment();
        lastSuccessNanos.accumulateAndGet(System.nanoTime(), Math::max);
    }

    private void recordExpectedFault(RuntimeException exception) {
        if (exception instanceof SplitBrainProtectionException) {
            protectionFailures.increment();
        } else if (exception instanceof OperationTimeoutException) {
            operationTimeouts.increment();
        } else {
            targetDisconnections.increment();
        }
        lastExpectedFaultNanos.accumulateAndGet(System.nanoTime(), Math::max);
        if (firstExpectedFault.compareAndSet(false, true)) {
            logger.warn("Retrying expected map fault [{}]: {}",
                    exception.getClass().getSimpleName(), exception.getMessage());
        }
    }

    private boolean pauseBeforeRetry() {
        if (isStopped()) {
            return false;
        }
        try {
            Thread.sleep(retryPauseMillis());
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            return false;
        }
        return !isStopped();
    }

    private boolean isStopped() {
        return Thread.currentThread().isInterrupted() || testContext != null && testContext.isStopped();
    }

    protected long retryPauseMillis() {
        return RETRY_PAUSE_MILLIS;
    }

    protected long minimumRecoveryMillis() {
        return MINIMUM_RECOVERY_MILLIS;
    }

    private RuntimeException findExpectedFault(Throwable throwable) {
        Throwable cause = unwrap(throwable);
        if (cause instanceof SplitBrainProtectionException || cause instanceof TargetDisconnectedException
                || cause instanceof OperationTimeoutException) {
            return (RuntimeException) cause;
        }
        return null;
    }

    private Throwable unwrap(Throwable throwable) {
        Throwable cause = throwable;
        while ((cause instanceof CompletionException || cause instanceof ExecutionException)
                && cause.getCause() != null) {
            cause = cause.getCause();
        }
        return cause;
    }
}
