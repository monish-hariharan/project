/*
  # Add booking management features

  1. Changes
    - Add cancellation_fee column to bookings table
    - Add refund_amount column to bookings table
    - Add cancelled_at column to bookings table
    - Add cancellation_reason column to bookings table
    
  2. Security
    - Maintains existing RLS policies
    - Adds new policy for updating bookings
*/

ALTER TABLE bookings 
ADD COLUMN cancellation_fee decimal(10,2),
ADD COLUMN refund_amount decimal(10,2),
ADD COLUMN cancelled_at timestamptz,
ADD COLUMN cancellation_reason text;

-- Allow users to update their own bookings
CREATE POLICY "Users can update their own bookings"
  ON bookings FOR UPDATE
  TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());