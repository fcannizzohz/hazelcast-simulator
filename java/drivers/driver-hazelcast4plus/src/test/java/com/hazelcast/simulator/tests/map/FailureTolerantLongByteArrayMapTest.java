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

import com.hazelcast.core.HazelcastInstance;
import com.hazelcast.core.OperationTimeoutException;
import com.hazelcast.map.IMap;
import com.hazelcast.simulator.common.TestCase;
import com.hazelcast.simulator.hazelcast4plus.HazelcastInstances;
import com.hazelcast.simulator.hz.HazelcastTest;
import com.hazelcast.simulator.test.annotations.TimeStep;
import com.hazelcast.simulator.worker.testcontainer.PropertyBinding;
import com.hazelcast.simulator.worker.testcontainer.TimeStepModel;
import com.hazelcast.splitbrainprotection.SplitBrainProtectionException;
import com.hazelcast.spi.exception.TargetDisconnectedException;
import org.junit.Before;
import org.junit.Test;
import org.mockito.ArgumentCaptor;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertSame;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.doNothing;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

public class FailureTolerantLongByteArrayMapTest {

    private TestSubject test;
    private IMap<Long, byte[]> map;
    private FailureTolerantLongByteArrayMap.ThreadState state;

    @Before
    @SuppressWarnings("unchecked")
    public void setUp() throws ReflectiveOperationException {
        HazelcastInstance instance = mock(HazelcastInstance.class);
        map = mock(IMap.class);
        when(instance.<Long, byte[]>getMap("map")).thenReturn(map);
        when(map.size()).thenReturn(1);

        test = new TestSubject();
        test.name = "map";
        test.keyDomain = 1;
        test.valueCount = 1;
        test.minValueLength = 1;
        test.maxValueLength = 1;
        test.maxOutstandingOperations = 1;
        test.setTargetInstance(instance);
        test.setUp();
        state = test.new ThreadState();
    }

    @Test
    public void getDropsSplitBrainProtectionFailure() {
        byte[] value = {42};
        when(map.getAsync(0L))
                .thenReturn(CompletableFuture.failedFuture(new SplitBrainProtectionException("expected")))
                .thenReturn(CompletableFuture.completedFuture(value));

        try { test.get(state).join(); fail("expected dropped operation"); }
        catch (CompletionException expected) { assertTrue(expected.getCause() instanceof SplitBrainProtectionException); }
        verify(map, times(1)).getAsync(0L);
        when(map.getAsync(0L)).thenReturn(CompletableFuture.completedFuture(value));
        assertArrayEquals(value, test.get(state).join());
        test.verifyFailureRecovery();
    }

    @Test
    public void setDropsTargetDisconnection() {
        when(map.setAsync(anyLong(), any(byte[].class)))
                .thenReturn(CompletableFuture.failedFuture(new TargetDisconnectedException("expected")))
                .thenReturn(CompletableFuture.completedFuture(null));

        try { test.set(state).join(); fail("expected dropped operation"); }
        catch (CompletionException expected) { assertTrue(expected.getCause() instanceof TargetDisconnectedException); }
        verify(map, times(1)).setAsync(anyLong(), any(byte[].class));
        when(map.setAsync(anyLong(), any(byte[].class))).thenReturn(CompletableFuture.completedFuture(null));
        test.set(state).join();
        test.verifyFailureRecovery();
    }

    @Test
    public void asynchronousGetDropsExpectedFailure() {
        byte[] value = {42};
        when(map.getAsync(0L))
                .thenReturn(CompletableFuture.failedFuture(new TargetDisconnectedException("expected")))
                .thenReturn(CompletableFuture.completedFuture(value));

        try { test.getAsync(state).join(); fail("expected dropped operation"); }
        catch (CompletionException expected) { assertTrue(expected.getCause() instanceof TargetDisconnectedException); }
        verify(map, times(1)).getAsync(0L);
        when(map.getAsync(0L)).thenReturn(CompletableFuture.completedFuture(value));
        assertArrayEquals(value, test.getAsync(state).join());
        test.verifyFailureRecovery();
    }

    @Test
    public void wrappedExpectedFailureIsDropped() {
        byte[] value = {42};
        when(map.getAsync(0L))
                .thenReturn(CompletableFuture.failedFuture(
                        new CompletionException(new OperationTimeoutException("expected"))))
                .thenReturn(CompletableFuture.completedFuture(value));

        try { test.get(state).join(); fail("expected dropped operation"); }
        catch (CompletionException expected) { assertTrue(expected.getCause() instanceof OperationTimeoutException); }
        verify(map, times(1)).getAsync(0L);
        when(map.getAsync(0L)).thenReturn(CompletableFuture.completedFuture(value));
        assertArrayEquals(value, test.get(state).join());
        test.verifyFailureRecovery();
    }

    @Test
    public void outstandingOperationsDrainWhenRunStops() {
        byte[] value = {42};
        CompletableFuture<byte[]> neverCompletes = new CompletableFuture<>();
        when(map.getAsync(0L))
                .thenReturn(CompletableFuture.completedFuture(value))
                .thenReturn(neverCompletes);

        test.get(state).join();
        CompletableFuture<byte[]> outstanding = test.get(state);
        test.drainOutstandingOperations();

        org.junit.Assert.assertTrue(outstanding.isCompletedExceptionally());
        test.verifyFailureRecovery();
    }

    @Test
    public void operationWithoutNativeAsyncApiFailsExplicitly() {
        try {
            test.getAll(state).join();
        } catch (CompletionException failure) {
            assertEquals(UnsupportedOperationException.class, failure.getCause().getClass());
            return;
        }
        throw new AssertionError("getAll should be disabled");
    }

    @Test
    public void simulatorBindsFailureToleranceProperties() {
        TestCase testCase = new TestCase("id")
                .setProperty("maxOutstandingOperations", 17)
                .setProperty("verifyMapSize", false)
                .setProperty("requireSuccessAfterFailure", false);
        TestSubject configured = new TestSubject();
        HazelcastInstances driver = new HazelcastInstances(List.of(mock(HazelcastInstance.class)));

        new PropertyBinding(testCase).setDriverInstance(driver).bind(configured);

        assertEquals(17, configured.maxOutstandingOperations);
        assertEquals(false, configured.verifyMapSize);
        assertEquals(false, configured.requireSuccessAfterFailure);
    }

    @Test
    public void mapSizeVerificationCanBeDisabled() {
        when(map.getAsync(0L)).thenReturn(CompletableFuture.completedFuture(new byte[]{42}));
        when(map.size()).thenReturn(0);
        test.verifyMapSize = false;

        test.get(state).join();

        test.verifyFailureRecovery();
    }

    @Test
    public void successAfterFailureRequirementCanBeDisabled() {
        when(map.getAsync(0L))
                .thenReturn(CompletableFuture.completedFuture(new byte[]{42}))
                .thenReturn(CompletableFuture.failedFuture(new OperationTimeoutException("expected")));
        test.get(state).join();
        CompletableFuture<byte[]> retrying = test.get(state);
        verify(map, times(2)).getAsync(0L);
        test.requireSuccessAfterFailure = false;
        test.drainOutstandingOperations();

        org.junit.Assert.assertTrue(retrying.isCompletedExceptionally());
        test.verifyFailureRecovery();
    }

    @Test(expected = IllegalArgumentException.class)
    public void nonPositiveOutstandingOperationLimitIsRejected() {
        TestSubject invalid = new TestSubject();
        invalid.maxOutstandingOperations = 0;
        invalid.setUp();
    }

    @Test
    public void outstandingOperationLimitDefaultsTo256() {
        assertEquals(256, new TestSubject().maxOutstandingOperations);
    }

    @Test
    public void blocksIssuerUntilOutstandingOperationCompletes() throws Exception {
        byte[] value = {42};
        CompletableFuture<byte[]> firstAttempt = new CompletableFuture<>();
        when(map.getAsync(0L))
                .thenReturn(firstAttempt)
                .thenReturn(CompletableFuture.completedFuture(value));

        CompletableFuture<byte[]> first = test.get(state);
        ExecutorService executor = Executors.newSingleThreadExecutor();
        CountDownLatch secondStarted = new CountDownLatch(1);
        try {
            Future<byte[]> second = executor.submit(() -> {
                secondStarted.countDown();
                return test.get(state).join();
            });

            assertTrue(secondStarted.await(1, TimeUnit.SECONDS));
            assertStillBlocked(second);
            verify(map, times(1)).getAsync(0L);

            firstAttempt.complete(value);
            assertArrayEquals(value, first.join());
            assertArrayEquals(value, second.get(2, TimeUnit.SECONDS));
            verify(map, times(2)).getAsync(0L);
            test.verifyFailureRecovery();
        } finally {
            firstAttempt.complete(value);
            executor.shutdownNow();
        }
    }

    @Test
    public void droppedOperationReleasesOutstandingPermit() throws Exception {
        byte[] value = {42};
        when(map.getAsync(0L))
                .thenReturn(CompletableFuture.failedFuture(new OperationTimeoutException("expected")))
                .thenReturn(CompletableFuture.completedFuture(value));

        CompletableFuture<byte[]> retrying = test.get(state);
        ExecutorService executor = Executors.newSingleThreadExecutor();
        CountDownLatch secondStarted = new CountDownLatch(1);
        try {
            Future<byte[]> second = executor.submit(() -> {
                secondStarted.countDown();
                return test.get(state).join();
            });

            assertTrue(secondStarted.await(1, TimeUnit.SECONDS));
            try { retrying.get(2, TimeUnit.SECONDS); fail("expected dropped operation"); }
            catch (ExecutionException expected) { assertTrue(expected.getCause() instanceof OperationTimeoutException); }
            assertArrayEquals(value, second.get(2, TimeUnit.SECONDS));
            verify(map, times(2)).getAsync(0L);
            test.verifyFailureRecovery();
        } finally {
            executor.shutdownNow();
        }
    }

    @Test
    public void stoppingTestWakesBlockedIssuerWithoutSubmittingIt() throws Exception {
        CompletableFuture<byte[]> firstAttempt = new CompletableFuture<>();
        when(map.getAsync(0L)).thenReturn(firstAttempt);

        CompletableFuture<byte[]> first = test.get(state);
        ExecutorService executor = Executors.newSingleThreadExecutor();
        CountDownLatch secondStarted = new CountDownLatch(1);
        try {
            Future<byte[]> second = executor.submit(() -> {
                secondStarted.countDown();
                return test.get(state).join();
            });
            assertTrue(secondStarted.await(1, TimeUnit.SECONDS));
            assertStillBlocked(second);

            test.drainOutstandingOperations();

            assertTrue(first.isCompletedExceptionally());
            try {
                second.get(2, TimeUnit.SECONDS);
                fail("blocked operation should be cancelled when the test stops");
            } catch (ExecutionException expected) {
                assertTrue(expected.getCause() instanceof java.util.concurrent.CancellationException);
            }
            verify(map, times(1)).getAsync(0L);
        } finally {
            executor.shutdownNow();
        }
    }

    @Test
    public void unexpectedFailureReleasesOutstandingPermit() {
        byte[] value = {42};
        when(map.getAsync(0L))
                .thenReturn(CompletableFuture.failedFuture(new IllegalStateException("unexpected")))
                .thenReturn(CompletableFuture.completedFuture(value));

        try {
            test.get(state).join();
            fail("first operation should fail");
        } catch (CompletionException expected) {
            assertTrue(expected.getCause() instanceof IllegalStateException);
        }

        assertArrayEquals(value, test.get(state).join());
        verify(map, times(2)).getAsync(0L);
    }

    @Test
    public void verificationFailureReleasesOutstandingPermit() {
        byte[] value = {42};
        when(map.getAsync(0L))
                .thenReturn(CompletableFuture.completedFuture(null))
                .thenReturn(CompletableFuture.completedFuture(value));

        try {
            test.get(state).join();
            fail("first operation should fail verification");
        } catch (CompletionException expected) {
            assertTrue(expected.getCause() instanceof AssertionError);
        }

        assertArrayEquals(value, test.get(state).join());
        verify(map, times(2)).getAsync(0L);
    }

    @Test
    public void exposesEveryBaselineOperationProperty() {
        Set<String> baselineOperations = new HashSet<>();
        for (Method method : LongByteArrayMapTest.class.getDeclaredMethods()) {
            if (method.isAnnotationPresent(TimeStep.class)) {
                baselineOperations.add(method.getName());
            }
        }
        Set<String> failureTolerantOperations = new HashSet<>();
        for (Method method : FailureTolerantLongByteArrayMap.class.getDeclaredMethods()) {
            if (method.isAnnotationPresent(TimeStep.class)) {
                failureTolerantOperations.add(method.getName());
            }
        }

        assertEquals(baselineOperations, failureTolerantOperations);
    }

    @Test
    public void simulatorCanConstructInheritedThreadState() throws Exception {
        TestCase testCase = new TestCase("id");
        TimeStepModel model = new TimeStepModel(FailureTolerantLongByteArrayMap.class,
                new PropertyBinding(testCase));

        assertEquals(AbstractLongByteArrayMapTest.class,
                model.getThreadStateConstructor("").getParameterTypes()[0]);
        model.getThreadStateConstructor("").newInstance(test);
    }

    @Test(expected = IllegalStateException.class)
    public void unexpectedExceptionRemainsFatal() {
        when(map.getAsync(0L)).thenReturn(CompletableFuture.failedFuture(new IllegalStateException("unexpected")));
        try {
            test.get(state).join();
        } catch (CompletionException failure) {
            throw (IllegalStateException) failure.getCause();
        }
    }

    @Test(expected = AssertionError.class)
    public void missingSuccessfulValueFailsCorrectnessCheck() {
        when(map.getAsync(0L)).thenReturn(CompletableFuture.completedFuture(null));
        try {
            test.get(state).join();
        } catch (CompletionException failure) {
            throw (AssertionError) failure.getCause();
        }
    }

    private static void awaitInvocations(Runnable assertion) {
        AssertionError last = null;
        for (int i = 0; i < 100; i++) {
            try {
                assertion.run();
                return;
            } catch (AssertionError failure) {
                last = failure;
                try {
                    Thread.sleep(10);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    throw failure;
                }
            }
        }
        throw last;
    }

    private static void assertStillBlocked(Future<?> future) throws Exception {
        assertFalse(future.isDone());
        try {
            future.get(200, TimeUnit.MILLISECONDS);
            fail("operation should still be waiting for outstanding-operation capacity");
        } catch (TimeoutException expected) {
            // Expected: the only capacity permit is still held by the first logical operation.
        }
    }

    private static final class TestSubject extends FailureTolerantLongByteArrayMap {

        private void setTargetInstance(HazelcastInstance instance) throws ReflectiveOperationException {
            targetInstance = instance;
            Field field = HazelcastTest.class.getDeclaredField("targetInstances");
            field.setAccessible(true);
            field.set(this, new HazelcastInstances(List.of(instance)));
        }

    }
}
