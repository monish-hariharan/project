import React from 'react';
import { motion } from 'framer-motion';
import { AlertTriangle, X } from 'lucide-react';
import { differenceInHours } from 'date-fns';

interface CancellationModalProps {
  startDate: string;
  onConfirm: () => void;
  onClose: () => void;
}

const CancellationModal: React.FC<CancellationModalProps> = ({
  startDate,
  onConfirm,
  onClose,
}) => {
  const hoursUntilStart = differenceInHours(new Date(startDate), new Date());
  const willIncurFee = hoursUntilStart < 24;

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center p-4 z-50"
    >
      <motion.div
        initial={{ scale: 0.9, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        className="bg-white rounded-lg max-w-md w-full p-6"
      >
        <div className="flex justify-between items-start mb-4">
          <div className="flex items-center space-x-2">
            <AlertTriangle className="h-6 w-6 text-yellow-500" />
            <h2 className="text-xl font-bold">Cancel Booking</h2>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-500"
          >
            <X className="h-6 w-6" />
          </button>
        </div>

        {willIncurFee ? (
          <div className="mb-6 p-4 bg-yellow-50 border border-yellow-200 rounded-lg">
            <p className="text-yellow-800">
              <strong>Warning:</strong> Cancelling this booking will incur a £100 fee as it's less than 24 hours before the rental start time.
            </p>
          </div>
        ) : (
          <p className="mb-6 text-gray-600">
            You will receive a full refund for this cancellation.
          </p>
        )}

        <div className="space-y-4">
          <button
            onClick={onConfirm}
            className="w-full bg-red-600 text-white py-2 rounded-lg hover:bg-red-700 transition-colors"
          >
            Confirm Cancellation
          </button>
          <button
            onClick={onClose}
            className="w-full border border-gray-300 text-gray-700 py-2 rounded-lg hover:bg-gray-50 transition-colors"
          >
            Keep Booking
          </button>
        </div>
      </motion.div>
    </motion.div>
  );
};

export default CancellationModal;