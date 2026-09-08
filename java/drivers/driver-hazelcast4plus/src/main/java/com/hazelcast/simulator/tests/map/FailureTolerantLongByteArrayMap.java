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

import com.hazelcast.client.HazelcastClientOfflineException;
import com.hazelcast.core.HazelcastException;
import com.hazelcast.core.OperationTimeoutException;
import com.hazelcast.map.IMap;
import com.hazelcast.simulator.test.annotations.AfterRun;
import com.hazelcast.simulator.test.annotations.Setup;
import com.hazelcast.simulator.test.annotations.Teardown;
import com.hazelcast.simulator.test.annotations.TimeStep;
import com.hazelcast.simulator.test.annotations.Verify;
import com.hazelcast.spi.exception.TargetDisconnectedException;
import com.hazelcast.splitbrainprotection.SplitBrainProtectionException;

import java.util.Map;
import java.util.Set;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.atomic.LongAdder;
import java.util.function.Consumer;
import java.util.function.Supplier;

/**
 * A native-async map workload which records transient client and cluster failures as drops.
 *
 * <p>Backpressure and recovery verification can be configured as normal Simulator test properties. For example:
 * <pre>
 * {@code
 *   maxOutstandingOperations: 256
 *   verifyMapSize: true
 *   requireSuccessAfterFailure: true
 * }
 * </pre>
 * Operation probabilities, key and value configuration, thread count, and rate remain independently configurable through
 * the properties inherited from {@link AbstractLongByteArrayMapTest} and the Simulator timestep runner.
 */
public class FailureTolerantLongByteArrayMap extends AbstractLongByteArrayMapTest {

    /** Maximum number of unresolved one-shot asynchronous invocations in this worker JVM. */
    public int maxOutstandingOperations = 256;

    /**
     * Whether verification checks that every configured map still contains exactly {@link #keyDomain} entries.
     *
     * <p>This should normally remain {@code true}, because the workload begins with the full key domain and its supported
     * operations are expected to preserve that domain. Set it to {@code false} only when the map can legitimately change
     * size, for example because the surrounding configuration enables TTL or eviction. Disabling it does not suppress
     * unexpected operation failures or outstanding-operation checks.
     */
    public boolean verifyMapSize = true;

    /**
     * Whether verification requires a successful logical operation after the most recently observed expected failure.
     *
     * <p>When {@code true}, a run which observes an expected client or cluster failure must also demonstrate recovery before
     * it can pass. Set it to {@code false} when a scenario intentionally ends while the cluster remains unavailable. The test
     * still requires at least one successful logical operation overall and still fails on unexpected exceptions.
     */
    public boolean requireSuccessAfterFailure = true;

    private Semaphore outstandingPermits;
    private final Set<CompletableFuture<?>> outstanding = java.util.concurrent.ConcurrentHashMap.newKeySet();
    private final LongAdder issuedOperations = new LongAdder();
    private final LongAdder completedOperations = new LongAdder();
    private final LongAdder droppedOperations = new LongAdder();
    private final LongAdder splitBrainDrops = new LongAdder();
    private final LongAdder clientOfflineDrops = new LongAdder();
    private final LongAdder targetDisconnectedDrops = new LongAdder();
    private final LongAdder operationTimeoutDrops = new LongAdder();
    private final LongAdder shutdownCancellations = new LongAdder();
    private final LongAdder backpressuredOperations = new LongAdder();
    private final AtomicInteger outstandingOperationCount = new AtomicInteger();
    private final AtomicInteger peakOutstandingOperations = new AtomicInteger();
    private final AtomicLong lastSuccessNanos = new AtomicLong(-1);
    private final AtomicLong lastDroppedNanos = new AtomicLong(-1);
    private final AtomicReference<Throwable> unexpectedFailure = new AtomicReference<>();
    private final AtomicBoolean stopping = new AtomicBoolean();

    @Override
    @Setup
    public void setUp() {
        if (maxOutstandingOperations < 1) {
            throw new IllegalArgumentException("maxOutstandingOperations must be at least 1: "
                    + maxOutstandingOperations);
        }
        super.setUp();
        outstandingPermits = new Semaphore(maxOutstandingOperations);
        logger.info("Failure-tolerant map configuration: maxOutstandingOperations={}, verifyMapSize={}, "
                        + "requireSuccessAfterFailure={}", maxOutstandingOperations, verifyMapSize,
                requireSuccessAfterFailure);
    }

    @TimeStep(prob = -1, countSuccessfulCompletions = true)
    public CompletableFuture<byte[]> get(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.fixedKeyOrRandom();
        return submit(() -> map.getAsync(key), value -> verifyValue(value, "get"));
    }

    /** Hazelcast has no native asynchronous getAll API. Keep this operation disabled. */
    @TimeStep(prob = 0, countSuccessfulCompletions = true)
    public CompletableFuture<Map<Long, byte[]>> getAll(ThreadState state) {
        return unsupportedAsyncOperation("getAll");
    }

    @TimeStep(prob = 0, countSuccessfulCompletions = true)
    public CompletableFuture<byte[]> getAsync(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        return submit(() -> map.getAsync(key), value -> verifyValue(value, "getAsync"));
    }

    @TimeStep(prob = 0.1, countSuccessfulCompletions = true)
    public CompletableFuture<byte[]> put(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return submit(() -> map.putAsync(key, value), previous -> verifyValue(previous, "put"));
    }

    @TimeStep(prob = 0)
    public CompletableFuture<Void> deleteAndPut(ThreadState state) {
        return unsupportedAsyncOperation("deleteAndPut");
    }

    @TimeStep(prob = 0.0, countSuccessfulCompletions = true)
    public CompletableFuture<byte[]> putAsync(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return submit(() -> map.putAsync(key, value), previous -> verifyValue(previous, "putAsync"));
    }

    @TimeStep(prob = 0, countSuccessfulCompletions = true)
    public CompletableFuture<Void> set(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return submit(() -> map.setAsync(key, value), ignored -> { });
    }

    @TimeStep(prob = 0, countSuccessfulCompletions = true)
    public CompletableFuture<Void> setAsync(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return submit(() -> map.setAsync(key, value), ignored -> { });
    }

    /** Hazelcast has no native asynchronous size API. Keep this operation disabled. */
    @TimeStep(prob = 0)
    public CompletableFuture<Integer> sizeLog() {
        return unsupportedAsyncOperation("sizeLog");
    }

    @TimeStep(prob = 0)
    public CompletableFuture<Map<Long, Object>> updateAllUsingEntryProcessor() {
        return unsupportedAsyncOperation("updateAllUsingEntryProcessor");
    }

    @TimeStep(prob = 0, countSuccessfulCompletions = true)
    public CompletableFuture<byte[]> pipelinedGet(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        return submit(() -> map.getAsync(key), value -> verifyValue(value, "pipelinedGet"));
    }

    @AfterRun
    public void drainOutstandingOperations() {
        if (stopping.compareAndSet(false, true)) {
            CancellationException cancellation = new CancellationException("Map workload stopped");
            outstanding.forEach(future -> {
                synchronized (future) {
                    shutdownCancellations.increment();
                    future.completeExceptionally(cancellation);
                }
            });
            logger.info("Failure-tolerant map stopped: issued={}, completed={}, dropped={}, "
                            + "splitBrainDrops={}, clientOfflineDrops={}, targetDisconnectedDrops={}, "
                            + "operationTimeoutDrops={}, shutdownCancellations={}, backpressured={}, "
                            + "peakOutstanding={}, outstanding={}", issuedOperations.sum(), completedOperations.sum(),
                    droppedOperations.sum(), splitBrainDrops.sum(), clientOfflineDrops.sum(),
                    targetDisconnectedDrops.sum(), operationTimeoutDrops.sum(), shutdownCancellations.sum(),
                    backpressuredOperations.sum(), peakOutstandingOperations.get(), outstandingOperationCount.get());
        }
    }

    @Verify(global = false)
    public void verifyFailureRecovery() {
        Throwable failure = unexpectedFailure.get();
        if (failure != null) {
            throw new AssertionError("An asynchronous map operation failed unexpectedly", failure);
        }
        if (!outstanding.isEmpty()) {
            throw new AssertionError(outstanding.size() + " logical map operations were still outstanding at verification");
        }
        if (completedOperations.sum() == 0) {
            throw new AssertionError("No logical map operation completed successfully");
        }
        if (requireSuccessAfterFailure && droppedOperations.sum() > 0
                && lastSuccessNanos.get() <= lastDroppedNanos.get()) {
            throw new AssertionError("No map operation succeeded after the final expected failure");
        }
        if (verifyMapSize) {
            verifyMapSizes();
        }
        logger.info("Failure-tolerant verification passed: issued={}, completed={}, dropped={}, "
                        + "splitBrainDrops={}, clientOfflineDrops={}, targetDisconnectedDrops={}, "
                        + "operationTimeoutDrops={}, shutdownCancellations={}, backpressured={}, "
                        + "peakOutstanding={}, outstanding={}", issuedOperations.sum(), completedOperations.sum(),
                droppedOperations.sum(), splitBrainDrops.sum(), clientOfflineDrops.sum(),
                targetDisconnectedDrops.sum(), operationTimeoutDrops.sum(), shutdownCancellations.sum(),
                backpressuredOperations.sum(), peakOutstandingOperations.get(), outstandingOperationCount.get());
    }

    @Override
    @Teardown
    public void tearDown() {
        drainOutstandingOperations();
        super.tearDown();
    }

    private <T> CompletableFuture<T> submit(Supplier<? extends CompletionStage<T>> operation, Consumer<T> verifier) {
        if (!acquireOutstandingPermit()) {
            CompletableFuture<T> cancelled = new CompletableFuture<>();
            cancelled.completeExceptionally(new CancellationException("Map workload stopped"));
            return cancelled;
        }

        issuedOperations.increment();
        CompletableFuture<T> result = new CompletableFuture<>();
        outstanding.add(result);
        int currentOutstanding = outstandingOperationCount.incrementAndGet();
        peakOutstandingOperations.accumulateAndGet(currentOutstanding, Math::max);
        result.whenComplete((ignored, failure) -> {
            if (outstanding.remove(result)) {
                outstandingOperationCount.decrementAndGet();
                outstandingPermits.release();
            }
        });

        // Shutdown may have started after the permit was acquired.  Do not
        // submit a new Hazelcast invocation once the result has been tracked
        // and teardown is in progress; otherwise the invocation could outlive
        // drainOutstandingOperations() and remain untracked.
        if (isStopping()) {
            shutdownCancellations.increment();
            result.completeExceptionally(new CancellationException("Map workload stopped"));
            return result;
        }

        CompletionStage<T> attempt;
        try {
            attempt = operation.get();
        } catch (Throwable failure) {
            handleFailure(result, failure);
            return result;
        }
        if (attempt == null) {
            completeUnexpected(result, new NullPointerException("Asynchronous map operation returned null"));
            return result;
        }
        attempt.whenComplete((value, failure) -> {
            if (failure != null) {
                handleFailure(result, failure);
                return;
            }
            synchronized (result) {
                if (result.isDone()) {
                    return;
                }
                try {
                    verifier.accept(value);
                    completedOperations.increment();
                    lastSuccessNanos.accumulateAndGet(System.nanoTime(), Math::max);
                    result.complete(value);
                } catch (Throwable verificationFailure) {
                    completeUnexpected(result, verificationFailure);
                }
            }
        });
        return result;
    }

    private <T> void handleFailure(CompletableFuture<T> result, Throwable failure) {
        synchronized (result) {
            if (result.isDone()) {
                return;
            }
            Throwable expected = findExpectedFailure(failure);
            if (expected == null) {
                completeUnexpected(result, unwrap(failure));
                return;
            }
            droppedOperations.increment();
            if (expected instanceof SplitBrainProtectionException) {
                splitBrainDrops.increment();
            } else if (expected instanceof HazelcastClientOfflineException) {
                clientOfflineDrops.increment();
            } else if (expected instanceof TargetDisconnectedException) {
                targetDisconnectedDrops.increment();
            } else if (expected instanceof OperationTimeoutException) {
                operationTimeoutDrops.increment();
            }
            lastDroppedNanos.accumulateAndGet(System.nanoTime(), Math::max);
            result.completeExceptionally(unwrap(failure));
        }
    }

    private <T> void completeUnexpected(CompletableFuture<T> result, Throwable failure) {
        synchronized (result) {
            if (!result.isDone()) {
                unexpectedFailure.compareAndSet(null, failure);
                result.completeExceptionally(failure);
            }
        }
    }

    private boolean acquireOutstandingPermit() {
        if (isStopping()) {
            return false;
        }
        try {
            if (outstandingPermits.tryAcquire()) {
                if (isStopping()) {
                    outstandingPermits.release();
                    return false;
                }
                return true;
            }

            backpressuredOperations.increment();
            while (!isStopping()) {
                if (outstandingPermits.tryAcquire(100, TimeUnit.MILLISECONDS)) {
                    if (isStopping()) {
                        outstandingPermits.release();
                        return false;
                    }
                    return true;
                }
            }
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
        return false;
    }

    private <T> CompletableFuture<T> unsupportedAsyncOperation(String operation) {
        UnsupportedOperationException failure = new UnsupportedOperationException(
                operation + " has no native asynchronous IMap API and is disabled in this failure-tolerant workload");
        unexpectedFailure.compareAndSet(null, failure);
        return CompletableFuture.failedFuture(failure);
    }

    private boolean isStopping() {
        return stopping.get() || testContext != null && testContext.isStopped();
    }

    private static Throwable findExpectedFailure(Throwable failure) {
        Throwable current = failure;
        while (current != null) {
            if (current instanceof SplitBrainProtectionException
                    || current instanceof HazelcastClientOfflineException
                    || current instanceof TargetDisconnectedException
                    || current instanceof OperationTimeoutException) {
                return current;
            }
            current = current.getCause();
        }
        return null;
    }

    private static Throwable unwrap(Throwable failure) {
        Throwable current = failure;
        while ((current instanceof CompletionException
                || current instanceof ExecutionException
                || current instanceof HazelcastException) && current.getCause() != null) {
            current = current.getCause();
        }
        return current;
    }
}
