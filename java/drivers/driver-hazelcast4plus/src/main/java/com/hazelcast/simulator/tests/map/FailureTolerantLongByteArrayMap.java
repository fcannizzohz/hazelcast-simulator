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
import com.hazelcast.core.HazelcastInstance;
import com.hazelcast.core.HazelcastException;
import com.hazelcast.core.OperationTimeoutException;
import com.hazelcast.map.EntryProcessor;
import com.hazelcast.map.IMap;
import com.hazelcast.simulator.hz.HazelcastTest;
import com.hazelcast.simulator.probes.LatencyProbe;
import com.hazelcast.simulator.test.BaseThreadState;
import com.hazelcast.simulator.test.annotations.Prepare;
import com.hazelcast.simulator.test.annotations.Setup;
import com.hazelcast.simulator.test.annotations.StartNanos;
import com.hazelcast.simulator.test.annotations.Teardown;
import com.hazelcast.simulator.test.annotations.TimeStep;
import com.hazelcast.simulator.test.annotations.Verify;
import com.hazelcast.simulator.worker.loadsupport.Streamer;
import com.hazelcast.simulator.worker.loadsupport.StreamerFactory;
import com.hazelcast.spi.exception.TargetDisconnectedException;
import com.hazelcast.splitbrainprotection.SplitBrainProtectionException;

import java.util.ArrayList;
import java.util.Collection;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.atomic.LongAdder;
import java.util.function.Consumer;
import java.util.function.Supplier;

import static com.hazelcast.simulator.tests.helpers.HazelcastTestUtils.assignKeyToIndex;
import static com.hazelcast.simulator.utils.GeneratorUtils.generateByteArrays;
import static java.lang.Thread.currentThread;

/**
 * A configurable map workload that keeps Simulator timestep threads free while
 * the Hazelcast client reconnects. Workload properties remain Simulator
 * properties; no rates or scenario-specific values are defined here.
 */
public class FailureTolerantLongByteArrayMap extends HazelcastTest {

    public int keyDomain = 10000;
    public int valueCount = 10000;
    public int minValueLength = 10;
    public int maxValueLength = 10;
    public int pipelineDepth = 10;
    public int pipelineIterations = 100;
    public int getAllSize = 5;
    public int mapCount = 1;
    public int fixedKeyDomain = 0;
    public int fixedKeyProbability = 0;

    private byte[][] values;
    private final List<List<IMap<Long, byte[]>>> maps = new ArrayList<>();
    private final Random random = new Random();
    private final Map<Thread, Integer> clientIndexForThread = new java.util.concurrent.ConcurrentHashMap<>();
    private final ExecutorService synchronousOperations = Executors.newCachedThreadPool(r -> {
        Thread thread = new Thread(r, "failure-tolerant-map-operation");
        thread.setDaemon(true);
        return thread;
    });
    private final ScheduledExecutorService retryScheduler = Executors.newScheduledThreadPool(2, r -> {
        Thread thread = new Thread(r, "failure-tolerant-map-retry");
        thread.setDaemon(true);
        return thread;
    });

    private final LongAdder issuedOperations = new LongAdder();
    private final LongAdder successfulOperations = new LongAdder();
    private final LongAdder retryAttempts = new LongAdder();
    private final LongAdder protectionFailures = new LongAdder();
    private final LongAdder targetDisconnections = new LongAdder();
    private final LongAdder operationTimeouts = new LongAdder();
    private final LongAdder pendingOperations = new LongAdder();
    private final AtomicLong lastSuccessNanos = new AtomicLong(-1);
    private final AtomicLong lastExpectedFaultNanos = new AtomicLong(-1);
    private final AtomicReference<Throwable> unexpectedFailure = new AtomicReference<>();
    private final AtomicBoolean firstExpectedFault = new AtomicBoolean();

    @Setup
    public void setUp() {
        for (HazelcastInstance instance : getTargetInstances()) {
            List<IMap<Long, byte[]>> instanceMaps = new ArrayList<>();
            maps.add(instanceMaps);
            for (int i = 0; i < mapCount; i++) {
                String mapName = mapCount == 1 ? name : name + "_" + i;
                instanceMaps.add(instance.getMap(mapName));
            }
        }
        values = generateByteArrays(valueCount, minValueLength, maxValueLength);
    }

    @Prepare(global = true)
    public void prepare() {
        for (IMap<Long, byte[]> map : maps.get(0)) {
            Streamer<Long, byte[]> streamer = StreamerFactory.getInstance(map);
            for (long key = 0; key < keyDomain; key++) {
                streamer.pushEntry(key, values[random.nextInt(valueCount)]);
            }
            streamer.await();
        }
    }

    @TimeStep(prob = -1)
    public CompletableFuture<byte[]> get(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.fixedKeyOrRandom();
        return retryAsync(() -> map.getAsync(key), value -> verifyValue(value, "get"));
    }

    @TimeStep(prob = 0)
    public CompletableFuture<Map<Long, byte[]>> getAll(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        Set<Long> keys = new HashSet<>();
        for (int k = 0; k < getAllSize; k++) {
            keys.add(state.randomKey());
        }
        return retrySyncAsync(() -> map.getAll(keys), result -> verifyGetAll(result, keys.size()));
    }

    @TimeStep(prob = 0)
    public CompletableFuture<byte[]> getAsync(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        return retryAsync(() -> map.getAsync(key), value -> verifyValue(value, "getAsync"));
    }

    @TimeStep(prob = 0.1)
    public CompletableFuture<byte[]> put(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return retryAsync(() -> map.putAsync(key, value), previous -> verifyValue(previous, "put"));
    }

    @TimeStep(prob = 0)
    public CompletableFuture<Void> deleteAndPut(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return retryAsync(() -> map.deleteAsync(key).thenCompose(ignored -> map.setAsync(key, value)), ignored -> { });
    }

    @TimeStep(prob = 0.0)
    public CompletableFuture<byte[]> putAsync(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return retryAsync(() -> map.putAsync(key, value), previous -> verifyValue(previous, "putAsync"));
    }

    @TimeStep(prob = 0)
    public CompletableFuture<Void> set(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return retryAsync(() -> map.setAsync(key, value), ignored -> { });
    }

    @TimeStep(prob = 0)
    public CompletableFuture<Void> setAsync(ThreadState state) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        byte[] value = state.randomValue();
        return retryAsync(() -> map.setAsync(key, value), ignored -> { });
    }

    @TimeStep(prob = 0)
    public CompletableFuture<Integer> sizeLog() {
        IMap<Long, byte[]> map = getRandomMap();
        return retrySyncAsync(map::size, size -> logger.info("current size of {}: {}", map.getName(), size));
    }

    @TimeStep(prob = 0)
    public CompletableFuture<Map<Long, Object>> updateAllUsingEntryProcessor() {
        IMap<Long, byte[]> map = getRandomMap();
        return retrySyncAsync(() -> map.executeOnEntries(new UpdateEntryProcessor((byte) 1)), ignored -> { });
    }

    @TimeStep(prob = 0)
    public CompletableFuture<Void> pipelinedGet(final ThreadState state, @StartNanos final long startNanos,
                                                final LatencyProbe probe) {
        IMap<Long, byte[]> map = getRandomMap();
        long key = state.randomKey();
        CompletableFuture<byte[]> operation = retryAsync(() -> map.getAsync(key),
                value -> verifyValue(value, "pipelinedGet"));
        operation.whenComplete((ignored, failure) -> probe.done(startNanos));
        return operation.thenApply(ignored -> null);
    }

    @Verify(global = false)
    public void verifyFailureRecovery() {
        Throwable failure = unexpectedFailure.get();
        if (failure != null) {
            throw new AssertionError("An asynchronous map operation failed unexpectedly", failure);
        }
        long pending = pendingOperations.sum();
        if (pending != 0) {
            throw new AssertionError(pending + " logical map operations were still pending at verification");
        }
        if (successfulOperations.sum() == 0) {
            throw new AssertionError("No map operation completed successfully");
        }
        if (protectionFailures.sum() + targetDisconnections.sum() + operationTimeouts.sum() > 0
                && lastSuccessNanos.get() <= lastExpectedFaultNanos.get()) {
            throw new AssertionError("No map operation succeeded after the final expected fault");
        }
        for (int mapIndex = 0; mapIndex < mapCount; mapIndex++) {
            String mapName = mapCount == 1 ? name : name + "_" + mapIndex;
            int actualSize = targetInstance.getMap(mapName).size();
            if (actualSize != keyDomain) {
                throw new AssertionError("Map [" + mapName + "] contains " + actualSize
                        + " entries; expected " + keyDomain);
            }
        }
        logger.info("Failure-tolerant verification passed: issued={}, successful={}, retries={}, pending={}, "
                        + "protectionFailures={}, targetDisconnections={}, operationTimeouts={}",
                issuedOperations.sum(), successfulOperations.sum(), retryAttempts.sum(), pending,
                protectionFailures.sum(), targetDisconnections.sum(), operationTimeouts.sum());
    }

    @Teardown
    public void tearDown() {
        retryScheduler.shutdownNow();
        synchronousOperations.shutdownNow();
        maps.stream().flatMap(Collection::stream).forEach(IMap::destroy);
    }

    private <T> CompletableFuture<T> retryAsync(Supplier<? extends CompletionStage<T>> operation,
                                                Consumer<T> verifier) {
        issuedOperations.increment();
        pendingOperations.increment();
        CompletableFuture<T> result = new CompletableFuture<>();
        attempt(operation, verifier, result);
        return result;
    }

    private <T> CompletableFuture<T> retrySyncAsync(Supplier<T> operation, Consumer<T> verifier) {
        return retryAsync(() -> CompletableFuture.supplyAsync(operation, synchronousOperations), verifier);
    }

    private <T> void attempt(Supplier<? extends CompletionStage<T>> operation, Consumer<T> verifier,
                             CompletableFuture<T> result) {
        if (isStopped()) {
            pendingOperations.decrement();
            result.complete(null);
            return;
        }
        try {
            operation.get().whenComplete((value, failure) -> {
                if (failure == null) {
                    try {
                        verifier.accept(value);
                        successfulOperations.increment();
                        lastSuccessNanos.accumulateAndGet(System.nanoTime(), Math::max);
                        pendingOperations.decrement();
                        result.complete(value);
                    } catch (Throwable verificationFailure) {
                        unexpectedFailure.compareAndSet(null, verificationFailure);
                        pendingOperations.decrement();
                        result.completeExceptionally(verificationFailure);
                    }
                    return;
                }
                RuntimeException expected = findExpectedFault(failure);
                if (expected == null) {
                    Throwable unwrapped = unwrap(failure);
                    unexpectedFailure.compareAndSet(null, unwrapped);
                    pendingOperations.decrement();
                    result.completeExceptionally(unwrapped);
                    return;
                }
                recordExpectedFault(expected);
                retryAttempts.increment();
                schedule(() -> attempt(operation, verifier, result));
            });
        } catch (RuntimeException failure) {
            RuntimeException expected = findExpectedFault(failure);
            if (expected == null) {
                unexpectedFailure.compareAndSet(null, failure);
                pendingOperations.decrement();
                result.completeExceptionally(failure);
                return;
            }
            recordExpectedFault(expected);
            retryAttempts.increment();
            schedule(() -> attempt(operation, verifier, result));
        }
    }

    private void schedule(Runnable retry) {
        if (!isStopped()) {
            try {
                retryScheduler.schedule(retry, 100, TimeUnit.MILLISECONDS);
            } catch (java.util.concurrent.RejectedExecutionException ignored) {
                // Test teardown can race with a completion callback.
            }
        }
    }

    private IMap<Long, byte[]> getRandomMap() {
        List<IMap<Long, byte[]>> mapsToSelectFrom;
        if (maps.size() == 1) {
            mapsToSelectFrom = maps.get(0);
        } else {
            Integer index = clientIndexForThread.get(currentThread());
            mapsToSelectFrom = maps.get(index == null ? putClientForCurrentThread() : index);
        }
        return mapsToSelectFrom.get(random.nextInt(mapCount));
    }

    private synchronized int putClientForCurrentThread() {
        return assignKeyToIndex(getTargetInstances().size(), currentThread(), clientIndexForThread);
    }

    private void verifyGetAll(Map<Long, byte[]> result, int expectedSize) {
        if (result.size() != expectedSize) {
            throw new AssertionError("getAll returned " + result.size() + " entries; expected " + expectedSize);
        }
        result.values().forEach(value -> verifyValue(value, "getAll"));
    }

    private void verifyValue(byte[] value, String operation) {
        if (value == null || value.length < minValueLength || value.length > maxValueLength) {
            throw new AssertionError(operation + " returned an invalid value");
        }
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
            logger.warn("Retrying expected map fault [{}]: {}", exception.getClass().getSimpleName(), exception.getMessage());
        }
    }

    private boolean isStopped() {
        return Thread.currentThread().isInterrupted() || testContext != null && testContext.isStopped();
    }

    private RuntimeException findExpectedFault(Throwable throwable) {
        Throwable cause = unwrap(throwable);
        if (cause instanceof SplitBrainProtectionException
                || cause instanceof TargetDisconnectedException
                || cause instanceof OperationTimeoutException
                || cause instanceof HazelcastClientOfflineException) {
            return (RuntimeException) cause;
        }
        return null;
    }

    private Throwable unwrap(Throwable throwable) {
        Throwable cause = throwable;
        while (!(cause instanceof SplitBrainProtectionException)
                && !(cause instanceof TargetDisconnectedException)
                && !(cause instanceof OperationTimeoutException)
                && !(cause instanceof HazelcastClientOfflineException)
                && (cause instanceof CompletionException || cause instanceof ExecutionException
                || cause instanceof HazelcastException)
                && cause.getCause() != null) {
            cause = cause.getCause();
        }
        return cause;
    }

    public class ThreadState extends BaseThreadState {
        protected long fixedKeyOrRandom() {
            if (fixedKeyDomain > 0 && fixedKeyDomain < keyDomain && fixedKeyProbability > 0
                    && randomInt(100) < fixedKeyProbability) {
                return randomLong(fixedKeyDomain);
            }
            return randomKey();
        }

        protected long randomKey() {
            return randomLong(keyDomain);
        }

        protected byte[] randomValue() {
            return values[randomInt(values.length)];
        }
    }

    private static final class UpdateEntryProcessor implements EntryProcessor<Long, byte[], Object> {
        private final byte increment;

        private UpdateEntryProcessor(byte increment) {
            this.increment = increment;
        }

        @Override
        public Object process(Map.Entry<Long, byte[]> entry) {
            byte[] value = entry.getValue();
            value[0] += increment;
            entry.setValue(value);
            return null;
        }
    }
}
