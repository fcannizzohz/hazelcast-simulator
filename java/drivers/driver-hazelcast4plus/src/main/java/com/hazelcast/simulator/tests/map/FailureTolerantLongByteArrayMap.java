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

import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ScheduledExecutorService;
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
 * A native-async map workload which retries transient client and cluster failures.
 *
 * <p>The retry policy and recovery verification can be configured as normal Simulator test properties. For example:
 * <pre>
 * {@code
 *   maxOutstandingOperations: 256
 *   retryDelayMillis: 100
 *   retrySchedulerThreadCount: 2
 *   verifyMapSize: true
 *   requireSuccessAfterFailure: true
 * }
 * </pre>
 * Operation probabilities, key and value configuration, thread count, and rate remain independently configurable through
 * the properties inherited from {@link AbstractLongByteArrayMapTest} and the Simulator timestep runner.
 */
public class FailureTolerantLongByteArrayMap extends AbstractLongByteArrayMapTest {

    /**
     * Maximum number of admitted logical operations which may be outstanding in this workload instance.
     *
     * <p>The default of {@code 256} bounds the memory retained by active Hazelcast invocations and operations waiting to
     * retry. The limit is shared by all timestep threads, maps, and target clients in one worker JVM. When it is reached,
     * the timestep thread blocks until an admitted logical operation completes or the test stops; the operation is not
     * submitted to Hazelcast before capacity is available. A retrying logical operation retains its capacity permit for
     * its entire lifetime, including the delay between attempts.
     *
     * <p>Use a lower value for large payloads or constrained client heaps. This setting bounds dynamic operation backlog,
     * but cannot prevent an out-of-memory error when static workload data such as {@code valueCount * value size} already
     * consumes most of the heap. Values smaller than {@code 1} are rejected during setup.
     */
    public int maxOutstandingOperations = 256;

    /**
     * Delay, in milliseconds, between a retryable failure and the next attempt of the same logical operation.
     *
     * <p>The default of {@code 100} avoids a tight retry loop while a client is offline or a cluster cannot satisfy
     * split-brain protection. Set this to {@code 0} for an immediate retry, which is useful in unit tests but can produce
     * substantial retry traffic in a failure experiment. Negative values are rejected during setup.
     */
    public long retryDelayMillis = 100;

    /**
     * Number of daemon threads used to schedule retry attempts.
     *
     * <p>These threads do not execute synchronous map operations: they only initiate native asynchronous Hazelcast calls.
     * Increasing this value can reduce retry-submission delay when many logical operations become eligible to retry at once.
     * Each logical operation still has at most one active attempt. Values smaller than {@code 1} are rejected during setup.
     */
    public int retrySchedulerThreadCount = 2;

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
     * Whether verification requires a successful logical operation after the most recently observed retryable failure.
     *
     * <p>When {@code true}, a run which observes an expected client or cluster failure must also demonstrate recovery before
     * it can pass. Set it to {@code false} when a scenario intentionally ends while the cluster remains unavailable. The test
     * still requires at least one successful logical operation overall and still fails on unexpected exceptions.
     */
    public boolean requireSuccessAfterFailure = true;

    private ScheduledExecutorService retryScheduler;
    private Semaphore outstandingPermits;
    private final Set<CompletableFuture<?>> outstanding = java.util.concurrent.ConcurrentHashMap.newKeySet();
    private final LongAdder issuedOperations = new LongAdder();
    private final LongAdder completedOperations = new LongAdder();
    private final LongAdder retries = new LongAdder();
    private final LongAdder expectedFailures = new LongAdder();
    private final LongAdder backpressuredOperations = new LongAdder();
    private final AtomicInteger outstandingOperationCount = new AtomicInteger();
    private final AtomicInteger peakOutstandingOperations = new AtomicInteger();
    private final AtomicLong lastSuccessNanos = new AtomicLong(-1);
    private final AtomicLong lastExpectedFailureNanos = new AtomicLong(-1);
    private final AtomicReference<Throwable> unexpectedFailure = new AtomicReference<>();
    private final AtomicBoolean stopping = new AtomicBoolean();
    private final AtomicBoolean firstExpectedFailure = new AtomicBoolean();

    @Override
    @Setup
    public void setUp() {
        if (maxOutstandingOperations < 1) {
            throw new IllegalArgumentException("maxOutstandingOperations must be at least 1: "
                    + maxOutstandingOperations);
        }
        if (retryDelayMillis < 0) {
            throw new IllegalArgumentException("retryDelayMillis must not be negative: " + retryDelayMillis);
        }
        if (retrySchedulerThreadCount < 1) {
            throw new IllegalArgumentException("retrySchedulerThreadCount must be at least 1: "
                    + retrySchedulerThreadCount);
        }
        super.setUp();
        outstandingPermits = new Semaphore(maxOutstandingOperations);
        retryScheduler = Executors.newScheduledThreadPool(retrySchedulerThreadCount, runnable -> {
            Thread thread = new Thread(runnable, "failure-tolerant-map-retry");
            thread.setDaemon(true);
            return thread;
        });
        logger.info("Failure-tolerant map configuration: maxOutstandingOperations={}, retryDelayMillis={}, "
                        + "retrySchedulerThreadCount={}, "
                        + "verifyMapSize={}, requireSuccessAfterFailure={}",
                maxOutstandingOperations, retryDelayMillis, retrySchedulerThreadCount,
                verifyMapSize, requireSuccessAfterFailure);
    }

    @TimeStep(prob = -1)
    public CompletableFuture<byte[]> get(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.fixedKeyOrRandom();
        return retry(() -> map.getAsync(key), value -> verifyValue(value, "get"));
    }

    /** Hazelcast has no native asynchronous getAll API. Keep this operation disabled. */
    @TimeStep(prob = 0)
    public CompletableFuture<Map<Long, byte[]>> getAll(ThreadState state) {
        return unsupportedAsyncOperation("getAll");
    }

    @TimeStep(prob = 0)
    public CompletableFuture<byte[]> getAsync(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        return retry(() -> map.getAsync(key), value -> verifyValue(value, "getAsync"));
    }

    @TimeStep(prob = 0.1)
    public CompletableFuture<byte[]> put(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return retry(() -> map.putAsync(key, value), previous -> verifyValue(previous, "put"));
    }

    @TimeStep(prob = 0)
    public CompletableFuture<Void> deleteAndPut(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return retry(() -> map.deleteAsync(key).thenCompose(ignored -> map.setAsync(key, value)), ignored -> { });
    }

    @TimeStep(prob = 0.0)
    public CompletableFuture<byte[]> putAsync(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return retry(() -> map.putAsync(key, value), previous -> verifyValue(previous, "putAsync"));
    }

    @TimeStep(prob = 0)
    public CompletableFuture<Void> set(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return retry(() -> map.setAsync(key, value), ignored -> { });
    }

    @TimeStep(prob = 0)
    public CompletableFuture<Void> setAsync(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return retry(() -> map.setAsync(key, value), ignored -> { });
    }

    /** Hazelcast has no native asynchronous size API. Keep this operation disabled. */
    @TimeStep(prob = 0)
    public CompletableFuture<Integer> sizeLog() {
        return unsupportedAsyncOperation("sizeLog");
    }

    @TimeStep(prob = 0)
    public CompletableFuture<Map<Long, Object>> updateAllUsingEntryProcessor() {
        IMap<Long, byte[]> map = getRandomMap();
        Set<Long> keys = new HashSet<>(keyDomain);
        for (long key = 0; key < keyDomain; key++) {
            keys.add(key);
        }
        return retry(() -> map.submitToKeys(keys, new UpdateEntryProcessor((byte) 1)), ignored -> { });
    }

    @TimeStep(prob = 0)
    public CompletableFuture<byte[]> pipelinedGet(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        return retry(() -> map.getAsync(key), value -> verifyValue(value, "pipelinedGet"));
    }

    @AfterRun
    public void drainOutstandingOperations() {
        if (stopping.compareAndSet(false, true)) {
            CancellationException cancellation = new CancellationException("Map workload stopped");
            outstanding.forEach(future -> {
                synchronized (future) {
                    future.completeExceptionally(cancellation);
                }
            });
            if (retryScheduler != null) {
                retryScheduler.shutdownNow();
            }
            logger.info("Failure-tolerant map stopped: issued={}, completed={}, retries={}, expectedFailures={}, "
                            + "backpressured={}, peakOutstanding={}, outstanding={}",
                    issuedOperations.sum(), completedOperations.sum(), retries.sum(), expectedFailures.sum(),
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
        if (requireSuccessAfterFailure && expectedFailures.sum() > 0
                && lastSuccessNanos.get() <= lastExpectedFailureNanos.get()) {
            throw new AssertionError("No map operation succeeded after the final expected failure");
        }
        if (verifyMapSize) {
            verifyMapSizes();
        }
        logger.info("Failure-tolerant verification passed: issued={}, completed={}, retries={}, expectedFailures={}, "
                        + "backpressured={}, peakOutstanding={}, outstanding={}",
                issuedOperations.sum(), completedOperations.sum(), retries.sum(), expectedFailures.sum(),
                backpressuredOperations.sum(), peakOutstandingOperations.get(), outstandingOperationCount.get());
    }

    @Override
    @Teardown
    public void tearDown() {
        drainOutstandingOperations();
        super.tearDown();
    }

    private <T> CompletableFuture<T> retry(Supplier<? extends CompletionStage<T>> operation, Consumer<T> verifier) {
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

        class Attempt implements Runnable {
            @Override
            public void run() {
                if (result.isDone()) {
                    return;
                }
                if (isStopping()) {
                    result.completeExceptionally(new CancellationException("Map workload stopped"));
                    return;
                }

                CompletionStage<T> attempt;
                try {
                    attempt = operation.get();
                } catch (Throwable failure) {
                    handleFailure(failure);
                    return;
                }
                if (attempt == null) {
                    completeUnexpected(new NullPointerException("Asynchronous map operation returned null"));
                    return;
                }
                attempt.whenComplete((value, failure) -> {
                    if (result.isDone()) {
                        return;
                    }
                    if (failure != null) {
                        handleFailure(failure);
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
                            completeUnexpected(verificationFailure);
                        }
                    }
                });
            }

            private void handleFailure(Throwable failure) {
                synchronized (result) {
                    if (result.isDone()) {
                        return;
                    }
                    Throwable expected = findExpectedFailure(failure);
                    if (expected == null) {
                        completeUnexpected(unwrap(failure));
                        return;
                    }
                    expectedFailures.increment();
                    lastExpectedFailureNanos.accumulateAndGet(System.nanoTime(), Math::max);
                    if (firstExpectedFailure.compareAndSet(false, true)) {
                        logger.warn("Retrying expected map failure [{}]: {}",
                                expected.getClass().getSimpleName(), expected.getMessage());
                    }
                    if (isStopping()) {
                        result.completeExceptionally(new CancellationException("Map workload stopped"));
                        return;
                    }
                    try {
                        retryScheduler.schedule(this, retryDelayMillis, TimeUnit.MILLISECONDS);
                        retries.increment();
                    } catch (RejectedExecutionException rejected) {
                        result.completeExceptionally(new CancellationException("Map workload stopped"));
                    }
                }
            }

            private void completeUnexpected(Throwable failure) {
                synchronized (result) {
                    if (!result.isDone()) {
                        unexpectedFailure.compareAndSet(null, failure);
                        result.completeExceptionally(failure);
                    }
                }
            }
        }

        new Attempt().run();
        return result;
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
